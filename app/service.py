"""Procesamiento de los eventos de Chatwoot (filtrado, deduplicado y ejecución del agente)."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .agent import Agent, ConversationActions, ConversationRef, HumanTookOver, RunResult, build_messages
from .chatwoot import INCOMING, ChatwootClient, ChatwootError, message_type_code
from .clients import ClientConfig, ClientNotFound, ClientRegistry
from .config import Settings
from .limits import RateLimiter
from .media import MediaProcessor, pending_incoming_ids
from .usage import UsageTracker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingEvent:
    account_id: int
    conversation_id: int
    message_id: int
    message: dict[str, Any]
    contact_name: str | None

    @property
    def key(self) -> tuple[int, int]:
        return (self.account_id, self.conversation_id)


class BotService:
    def __init__(
        self,
        settings: Settings,
        registry: ClientRegistry,
        chatwoot: ChatwootClient,
        agent: Agent,
        *,
        media: MediaProcessor | None = None,
        usage: UsageTracker | None = None,
        limiter: RateLimiter | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.chatwoot = chatwoot
        self.agent = agent
        self.media = media
        self.usage = usage
        self.limiter = limiter or RateLimiter()
        self._runs = 0
        self._latest: dict[tuple[int, int], int] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._warned_history = False

    # --- Filtro (se ejecuta dentro del request, debe ser rápido) ---------------------

    def accept(self, payload: dict[str, Any]) -> IncomingEvent | None:
        """Devuelve el evento a procesar, o None si hay que ignorarlo."""
        if payload.get("event") != "message_created":
            return None
        if message_type_code(payload.get("message_type")) != INCOMING or payload.get("private"):
            return None

        conversation = payload.get("conversation") or {}
        try:
            account_id = int((payload.get("account") or {})["id"])
            conversation_id = int(conversation["id"])
            message_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Evento message_created con formato inesperado; se ignora")
            return None

        if conversation.get("status") == "open":
            logger.debug("Conversación %s atendida por un humano; se ignora", conversation_id)
            return None
        try:
            self.registry.get(account_id)
        except ClientNotFound:
            logger.warning("Mensaje de la cuenta %s sin configuración en %s; se ignora",
                           account_id, self.registry.directory)
            return None

        if message_id in self._seen:  # Chatwoot reintenta entregas ante errores 429/500
            return None
        self._seen[message_id] = None
        if len(self._seen) > 10_000:
            self._seen.popitem(last=False)

        key = (account_id, conversation_id)
        self._latest[key] = max(self._latest.get(key, 0), message_id)
        sender = payload.get("sender") or {}
        return IncomingEvent(account_id, conversation_id, message_id, payload, sender.get("name"))

    # --- Procesamiento en segundo plano ---------------------------------------------

    async def process(self, event: IncomingEvent) -> None:
        # En WhatsApp es común mandar varios mensajes seguidos: esperamos un momento y
        # respondemos solo al último, que ya incluye a los anteriores en el historial.
        if self.settings.debounce_seconds > 0:
            await asyncio.sleep(self.settings.debounce_seconds)
        if self._latest.get(event.key) != event.message_id:
            return

        lock = self._locks.setdefault(event.key, asyncio.Lock())
        try:
            async with lock:
                if self._latest.get(event.key) != event.message_id:
                    return
                await self._handle(event)
        finally:
            if not lock.locked() and self._latest.get(event.key) == event.message_id:
                self._locks.pop(event.key, None)
                self._latest.pop(event.key, None)

    async def _handle(self, event: IncomingEvent) -> None:
        try:
            client = self.registry.get(event.account_id)
        except Exception:
            logger.exception("No se pudo cargar la configuración de la cuenta %s", event.account_id)
            return
        ref = ConversationRef(event.account_id, event.conversation_id, client.bot_token(self.settings.chatwoot_bot_token))
        try:
            conversation = await self.chatwoot.get_conversation(ref.bot_token, ref.account_id, ref.conversation_id)
            if conversation.get("status") == "open":
                logger.info("Conversación %s tomada por un humano; el bot no responde", ref.conversation_id)
                return

            history = await self._history(client, event)
            if not pending_incoming_ids(history):
                logger.info("Conversación %s sin mensajes pendientes de respuesta", ref.conversation_id)
                return

            limit_reason = self._check_limits(client, event)
            if limit_reason:
                logger.warning("Cuenta %s conv %s: %s", ref.account_id, ref.conversation_id, limit_reason)
                result = await self._handoff(client, ref, limit_reason)
                await self._record_run(ref, "rate_limited", result)
                return

            attachments = (
                await self.media.render(history, ref.account_id, ref.conversation_id) if self.media else None
            )
            messages = build_messages(history, attachments)
            if not messages or messages[-1]["role"] != "user":
                logger.info("Conversación %s sin mensajes pendientes de respuesta", ref.conversation_id)
                return

            result = await self.agent.run(
                client,
                ref,
                messages,
                contact_name=event.contact_name,
                labels=list(conversation.get("labels") or []),
            )
            logger.info(
                "Cuenta %s conv %s: respuestas=%d derivada=%s venta=%s abortado=%s",
                ref.account_id, ref.conversation_id, len(result.replies), result.handed_off,
                result.interest_registered, result.aborted_reason,
            )
            outcome = result.aborted_reason or ("handed_off" if result.handed_off else "replied")
            await self._record_run(ref, outcome, result)
        except Exception:
            logger.exception("Error procesando mensaje %s (cuenta %s, conv %s)",
                             event.message_id, ref.account_id, ref.conversation_id)
            result = await self._handoff(client, ref, "Error técnico del asistente automático (ver logs del bot).")
            await self._record_run(ref, "error", result)

    def _check_limits(self, client: ClientConfig, event: IncomingEvent) -> str | None:
        """Devuelve el motivo si se superó algún límite de respuestas automáticas."""
        self._runs += 1
        if self._runs % 500 == 0:
            self.limiter.prune(86_400)
        por_hora = client.limites.respuestas_por_conversacion_por_hora
        if por_hora is None:
            por_hora = self.settings.rate_limit_conversation_per_hour
        por_dia = client.limites.respuestas_por_dia
        if por_dia is None:
            por_dia = self.settings.rate_limit_account_per_day

        if not self.limiter.allow(("conversacion", event.key), por_hora, 3600):
            return (f"La conversación superó el límite de {por_hora} respuestas automáticas por hora "
                    "(posible spam o una consulta que conviene atender en persona).")
        if not self.limiter.allow(("cuenta", event.account_id), por_dia, 86_400):
            return f"La cuenta alcanzó el límite de {por_dia} respuestas automáticas en 24 horas."
        return None

    async def _record_run(self, ref: ConversationRef, outcome: str, result: RunResult | None) -> None:
        if not self.usage:
            return
        await self.usage.record(
            "run",
            ref.account_id,
            ref.conversation_id,
            outcome=outcome,
            replies=len(result.replies) if result else 0,
            handed_off=bool(result and result.handed_off),
            interest_registered=bool(result and result.interest_registered),
        )

    async def _history(self, client: ClientConfig, event: IncomingEvent) -> list[dict[str, Any]]:
        token = client.api_token(self.settings.chatwoot_api_token) or client.bot_token(self.settings.chatwoot_bot_token)
        try:
            history = await self.chatwoot.list_messages(token, event.account_id, event.conversation_id)
        except ChatwootError as exc:
            if not self._warned_history:
                logger.warning(
                    "No se pudo leer el historial (%s). Los tokens de Agent Bot no pueden listar mensajes: "
                    "definí CHATWOOT_API_TOKEN con el token de un usuario administrador. Se responde solo "
                    "con el último mensaje.", exc,
                )
                self._warned_history = True
            history = []

        history = history[-self.settings.history_limit:]
        if all(int(m.get("id", 0)) != event.message_id for m in history):
            history.append(event.message)
        return history

    async def _handoff(self, client: ClientConfig, ref: ConversationRef, motivo: str) -> RunResult | None:
        # Ante errores o límites, es mejor pasar la conversación a una persona que dejar al
        # cliente sin respuesta.
        actions = ConversationActions(self.chatwoot, client, ref)
        try:
            await actions.handoff(motivo)
        except HumanTookOver:
            actions.result.aborted_reason = "human_took_over"
        except Exception:
            logger.exception("No se pudo derivar la conversación %s", ref.conversation_id)
            return None
        return actions.result
