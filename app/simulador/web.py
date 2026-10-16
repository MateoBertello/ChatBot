"""Servidor del simulador: una página con el chat de WhatsApp y el panel de Chatwoot."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from ..agent import Agent
from ..clients import ClientNotFound, ClientRegistry
from ..config import Settings
from ..llm import build_llm, provider_name
from ..media import MediaProcessor
from ..service import BotService
from ..transcription import build_transcriber
from ..usage import UsageTracker
from .chatwoot_simulado import Archivo, ChatwootSimulado

logger = logging.getLogger(__name__)

BASE_URL = "http://simulador.local"
PAGINA = Path(__file__).with_name("pagina.html")
MAX_ARCHIVO = 25 * 1024 * 1024


class ConsumoEnMemoria(UsageTracker):
    """Guarda el consumo por conversación en memoria en lugar de un archivo."""

    def __init__(self) -> None:
        super().__init__(None)
        self.registros: dict[tuple[int, int | None], list[dict[str, Any]]] = defaultdict(list)

    async def record(self, kind: str, account_id: int, conversation_id: int | None = None, **fields: Any) -> None:
        self.registros[(account_id, conversation_id)].append({"type": kind, **fields})

    def resumen(self, account_id: int, conversation_id: int) -> dict[str, Any]:
        regs = self.registros.get((account_id, conversation_id), [])
        claude = [r for r in regs if r["type"] == "claude"]
        costos = [r["cost_usd"] for r in claude if r.get("cost_usd") is not None]
        return {
            "llamadas_claude": len(claude),
            "input_tokens": sum(r.get("input_tokens", 0) for r in claude),
            "output_tokens": sum(r.get("output_tokens", 0) for r in claude),
            "cache_read_input_tokens": sum(r.get("cache_read_input_tokens", 0) for r in claude),
            "costo_estimado_usd": round(sum(costos), 4),
            "transcripciones": sum(1 for r in regs if r["type"] == "transcription"),
            "modelo": claude[-1].get("model") if claude else None,
        }


def create_sim_app(settings: Settings, llm: Any | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await media_http.aclose()
        await transcription_http.aclose()
        close = getattr(llm, "close", None)
        if close is not None:
            await close()

    app = FastAPI(title="Simulador del bot", lifespan=lifespan)
    chatwoot = ChatwootSimulado(BASE_URL)
    consumo = ConsumoEnMemoria()
    registry = ClientRegistry(settings.clients_dir)
    llm = llm or build_llm(settings)

    # Los adjuntos se "descargan" de este mismo servidor, sin pasar por la red.
    media_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    transcription_http = httpx.AsyncClient(timeout=60.0)
    media = MediaProcessor(
        media_http,
        allowed_hosts=("simulador.local",),
        transcriber=build_transcriber(settings, transcription_http),
        usage=consumo,
        max_media_per_run=settings.media_max_per_run,
    )
    service = BotService(
        settings, registry, chatwoot, Agent(llm, chatwoot, settings, usage=consumo), media=media, usage=consumo
    )
    en_proceso: dict[tuple[int, int], int] = defaultdict(int)
    tareas: set[asyncio.Task] = set()

    app.state.chatwoot = chatwoot
    app.state.service = service

    async def procesar(clave: tuple[int, int], evento: Any) -> None:
        en_proceso[clave] += 1
        try:
            await service.process(evento)
        finally:
            en_proceso[clave] -= 1

    def conversacion_o_404(account_id: int, conversation_id: int):
        conv = chatwoot.conversaciones.get((account_id, conversation_id))
        if conv is None:
            raise HTTPException(404, "No existe esa conversación")
        return conv

    @app.get("/", response_class=HTMLResponse)
    async def pagina() -> str:
        return PAGINA.read_text(encoding="utf-8")

    @app.get("/sim/api/info")
    async def info() -> dict[str, Any]:
        clientes = []
        for path in sorted(settings.clients_dir.glob("*.y*ml")):
            if not path.stem.isdigit():
                continue
            try:
                cfg = registry.get(int(path.stem))
                clientes.append({"account_id": cfg.account_id, "nombre": cfg.negocio.nombre})
            except (ClientNotFound, ValueError, yaml.YAMLError) as exc:
                clientes.append({"account_id": int(path.stem), "nombre": None, "error": str(exc)[:300]})
        return {
            "clientes": clientes,
            "modelo": settings.claude_model,
            "proveedor": provider_name(settings),
            "esfuerzo": settings.claude_effort or "por defecto del modelo",
            "transcripcion": settings.transcription_provider if media.transcriber else None,
            "espera_segundos": settings.debounce_seconds,
            "carpeta_clientes": str(settings.clients_dir),
        }

    @app.post("/sim/api/conversaciones")
    async def nueva_conversacion(account_id: int = Form(...), contacto: str = Form("Cliente de prueba")) -> dict:
        try:
            registry.get(account_id)
        except ClientNotFound:
            raise HTTPException(404, f"No hay clients/{account_id}.yaml") from None
        conv = chatwoot.nueva_conversacion(account_id, contacto.strip() or "Cliente de prueba")
        return {"account_id": account_id, "id": conv.id}

    @app.get("/sim/api/conversaciones/{account_id}/{conversation_id}")
    async def estado(account_id: int, conversation_id: int) -> dict[str, Any]:
        conv = conversacion_o_404(account_id, conversation_id)
        return {
            "account_id": account_id,
            "id": conv.id,
            "contacto": conv.contacto,
            "status": conv.status,
            "labels": conv.labels,
            "mensajes": conv.mensajes,
            "procesando": en_proceso[(account_id, conversation_id)] > 0,
            "consumo": consumo.resumen(account_id, conversation_id),
        }

    @app.post("/sim/api/conversaciones/{account_id}/{conversation_id}/mensajes")
    async def mensaje_cliente(
        account_id: int,
        conversation_id: int,
        texto: str = Form(""),
        archivo: UploadFile | None = File(None),
        esperar: bool = False,
    ) -> dict[str, Any]:
        conv = conversacion_o_404(account_id, conversation_id)
        adjunto = None
        if archivo is not None and archivo.filename:
            contenido = await archivo.read()
            if len(contenido) > MAX_ARCHIVO:
                raise HTTPException(413, "El archivo supera los 25 MB")
            adjunto = Archivo(contenido, archivo.content_type or "application/octet-stream", archivo.filename)
        texto = texto.strip()
        if not texto and adjunto is None:
            raise HTTPException(400, "Escribí un mensaje o adjuntá un archivo")

        payload = chatwoot.mensaje_del_cliente(conv, texto, adjunto)
        evento = service.accept(payload)
        if evento is None:
            motivo = ("La conversación está Abierta (la atiende una persona): el bot no responde."
                      if conv.status == "open" else "El bot ignoró el mensaje.")
            return {"status": "ignored", "motivo": motivo}

        clave = (account_id, conversation_id)
        if esperar:
            await procesar(clave, evento)
        else:
            tarea = asyncio.create_task(procesar(clave, evento))
            tareas.add(tarea)
            tarea.add_done_callback(tareas.discard)
        return {"status": "accepted"}

    @app.post("/sim/api/conversaciones/{account_id}/{conversation_id}/agente")
    async def mensaje_agente(account_id: int, conversation_id: int, texto: str = Form(...)) -> dict:
        conv = conversacion_o_404(account_id, conversation_id)
        if not texto.strip():
            raise HTTPException(400, "El mensaje está vacío")
        chatwoot.mensaje_del_agente(conv, texto.strip())
        return {"ok": True}

    @app.post("/sim/api/conversaciones/{account_id}/{conversation_id}/estado")
    async def cambiar_estado(account_id: int, conversation_id: int, status: str = Form(...)) -> dict:
        conv = conversacion_o_404(account_id, conversation_id)
        try:
            chatwoot.cambiar_estado(conv, status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"status": conv.status}

    @app.get("/sim/archivos/{archivo_id}")
    async def archivo(archivo_id: int) -> Response:
        arch = chatwoot.archivos.get(archivo_id)
        if arch is None:
            raise HTTPException(404, "No existe el archivo")
        return Response(arch.contenido, media_type=arch.content_type)

    return app
