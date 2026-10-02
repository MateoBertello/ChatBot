"""Servicio FastAPI que actúa como Agent Bot de Chatwoot."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import anthropic
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .agent import Agent
from .chatwoot import ChatwootClient
from .clients import ClientRegistry
from .config import get_settings
from .media import MediaProcessor
from .service import BotService
from .transcription import build_transcriber
from .usage import UsageTracker

logger = logging.getLogger("chatbot")

SIGNATURE_MAX_AGE_SECONDS = 300


def verify_webhook(headers: Any, query_token: str | None, body: bytes, secret: str, now: float | None = None) -> bool:
    """Valida el webhook con el secreto compartido.

    Se acepta cualquiera de estas formas:
    - token en la URL configurada en Chatwoot: ``/webhook?token=<WEBHOOK_SECRET>``;
    - header ``X-Webhook-Token: <WEBHOOK_SECRET>`` o ``Authorization: Bearer <WEBHOOK_SECRET>``;
    - firma HMAC que Chatwoot agrega cuando el bot tiene secreto
      (``X-Chatwoot-Signature: sha256=HMAC(secret, "<timestamp>.<body>")``).
    """
    if not secret:
        return False

    candidates = [query_token, headers.get("x-webhook-token")]
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    for candidate in candidates:
        if candidate and hmac.compare_digest(candidate.encode(), secret.encode()):
            return True

    signature = headers.get("x-chatwoot-signature")
    timestamp = headers.get("x-chatwoot-timestamp")
    if signature and timestamp:
        try:
            age = abs((now or time.time()) - int(timestamp))
        except ValueError:
            return False
        if age > SIGNATURE_MAX_AGE_SECONDS:
            return False
        expected = "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    return False


def build_service() -> tuple[BotService, list[Any]]:
    settings = get_settings()
    chatwoot = ChatwootClient(settings.chatwoot_url)
    llm = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    usage = UsageTracker(settings.usage_file)
    media_http = httpx.AsyncClient(timeout=60.0)
    chatwoot_host = urlparse(settings.chatwoot_url).hostname or ""
    media = MediaProcessor(
        media_http,
        allowed_hosts=(chatwoot_host, *settings.media_allowed_hosts),
        transcriber=build_transcriber(settings, media_http),
        usage=usage,
        max_media_per_run=settings.media_max_per_run,
    )
    service = BotService(
        settings,
        ClientRegistry(settings.clients_dir),
        chatwoot,
        Agent(llm, chatwoot, settings, usage=usage),
        media=media,
        usage=usage,
    )
    return service, [chatwoot, llm, media_http]


def create_app(service: BotService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        closables: list[Any] = []
        settings = app.state.service.settings if app.state.service else get_settings()
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if app.state.service is None:
            app.state.service, closables = build_service()
        yield
        for resource in closables:
            await (resource.aclose() if hasattr(resource, "aclose") else resource.close())

    app = FastAPI(title="Chatwoot Agent Bot", lifespan=lifespan)
    app.state.service = service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/usage")
    async def usage(request: Request, mes: str | None = None, account_id: int | None = None) -> dict[str, Any]:
        """Consumo por cuenta (para facturar). Requiere ADMIN_TOKEN; `mes` con formato YYYY-MM."""
        svc: BotService = request.app.state.service
        admin_token = svc.settings.admin_token
        if not admin_token or svc.usage is None:
            raise HTTPException(status_code=404, detail="not found")
        auth = request.headers.get("authorization") or ""
        provided = request.query_params.get("token") or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
        if not hmac.compare_digest(provided.encode(), admin_token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")
        if mes and not re.fullmatch(r"\d{4}-\d{2}", mes):
            raise HTTPException(status_code=400, detail="mes debe tener formato YYYY-MM")
        return await asyncio.to_thread(svc.usage.summarize, mes, account_id)

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> dict[str, str]:
        svc: BotService = request.app.state.service
        body = await request.body()
        if not verify_webhook(request.headers, request.query_params.get("token"), body, svc.settings.webhook_secret):
            logger.warning("Webhook rechazado: token o firma inválidos (%s)", request.client.host if request.client else "?")
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")

        event = svc.accept(payload)
        if event is None:
            return {"status": "ignored"}
        # Respondemos enseguida a Chatwoot y generamos la respuesta en segundo plano.
        background.add_task(svc.process, event)
        return {"status": "accepted"}

    return app


app = create_app()
