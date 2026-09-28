"""Agente con Claude: arma el contexto, llama a la API con tool use y ejecuta las herramientas."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .chatwoot import INCOMING, OUTGOING, TEMPLATE, ChatwootClient, ChatwootError, message_type_code
from .clients import ClientConfig
from .config import Settings
from .prompts import build_system_prompt, build_turn_context

logger = logging.getLogger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "responder",
        "description": (
            "Envía un mensaje de WhatsApp al cliente en esta conversación. Es la única forma de que el "
            "cliente vea tu respuesta. Usala para contestar consultas, pedir datos o confirmar pasos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Mensaje para el cliente, en formato WhatsApp."}
            },
            "required": ["texto"],
        },
    },
    {
        "name": "derivar_a_humano",
        "description": (
            "Pasa la conversación a una persona del equipo: deja una nota privada con el motivo, marca la "
            "conversación como abierta para los agentes y le avisa al cliente que lo atiende una persona. "
            "Usala cuando el cliente lo pide, ante reclamos o cuando la respuesta no está en la base de "
            "conocimiento. Después de derivar ya no vas a seguir en esta conversación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Motivo concreto de la derivación, para que el agente humano tenga contexto.",
                }
            },
            "required": ["motivo"],
        },
    },
    {
        "name": "registrar_interes_de_compra",
        "description": (
            "Registra que el cliente tiene intención concreta de comprar: agrega la etiqueta de venta a la "
            "conversación y deja una nota privada con los datos para que el equipo de ventas haga el "
            "seguimiento. No le envía nada al cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Producto o servicio de interés."},
                "datos": {
                    "type": "object",
                    "description": "Datos del potencial cliente que surjan de la conversación. Omití los que no dio.",
                    "properties": {
                        "nombre": {"type": "string"},
                        "telefono": {"type": "string"},
                        "email": {"type": "string"},
                        "cantidad": {"type": "string"},
                        "entrega": {"type": "string", "description": "Retiro, envío, dirección, fecha, etc."},
                        "comentarios": {"type": "string"},
                    },
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["producto", "datos"],
        },
    },
]

FALLBACKS_BETA = "server-side-fallback-2026-07-01"


@dataclass
class ConversationRef:
    account_id: int
    conversation_id: int
    bot_token: str


@dataclass
class RunResult:
    replies: list[str] = field(default_factory=list)
    handed_off: bool = False
    interest_registered: bool = False
    aborted_reason: str | None = None


class HumanTookOver(Exception):
    """La conversación pasó a un humano mientras el bot generaba la respuesta."""


class ConversationActions:
    """Acciones que el agente puede ejecutar sobre una conversación de Chatwoot."""

    def __init__(self, chatwoot: ChatwootClient, client: ClientConfig, ref: ConversationRef):
        self.chatwoot = chatwoot
        self.client = client
        self.ref = ref
        self.result = RunResult()
        self._ownership_checked = False

    async def _ensure_bot_still_owns(self) -> None:
        # Generar la respuesta tarda unos segundos: verificamos que ningún humano haya
        # tomado la conversación antes de enviarle algo al cliente.
        if self._ownership_checked:
            return
        conv = await self.chatwoot.get_conversation(self.ref.bot_token, self.ref.account_id, self.ref.conversation_id)
        if conv.get("status") == "open":
            raise HumanTookOver()
        self._ownership_checked = True

    async def reply(self, texto: str) -> str:
        if self.result.handed_off:
            return "La conversación ya fue derivada a una persona; no se envió el mensaje."
        await self._ensure_bot_still_owns()
        await self.chatwoot.send_message(self.ref.bot_token, self.ref.account_id, self.ref.conversation_id, texto)
        self.result.replies.append(texto)
        return "Mensaje enviado al cliente."

    async def handoff(self, motivo: str) -> str:
        if self.result.handed_off:
            return "La conversación ya estaba derivada."
        await self._ensure_bot_still_owns()
        humana = self.client.atencion_humana
        aviso = humana.mensaje_derivacion if humana.disponible() else humana.mensaje_derivacion_fuera_de_horario
        acc, conv, token = self.ref.account_id, self.ref.conversation_id, self.ref.bot_token

        errores = []
        for descripcion, accion in (
            ("nota privada", lambda: self.chatwoot.send_message(token, acc, conv, f"🤖 Derivado por el bot. Motivo: {motivo}", private=True)),
            ("aviso al cliente", lambda: self.chatwoot.send_message(token, acc, conv, aviso)),
            ("cambio de estado", lambda: self.chatwoot.set_status(token, acc, conv, "open")),
        ):
            try:
                await accion()
            except ChatwootError:
                logger.exception("Error en derivación (%s) cuenta=%s conversación=%s", descripcion, acc, conv)
                errores.append(descripcion)

        self.result.handed_off = True
        self.result.replies.append(aviso)
        if errores:
            return "Derivación parcial; fallaron: " + ", ".join(errores)
        return "Conversación derivada a una persona. El cliente ya fue avisado."

    async def register_interest(self, producto: str, datos: dict[str, Any]) -> str:
        acc, conv, token = self.ref.account_id, self.ref.conversation_id, self.ref.bot_token
        await self.chatwoot.add_labels(token, acc, conv, [self.client.etiqueta_venta])
        lineas = ["🛒 Interés de compra registrado por el bot", f"Producto: {producto}"]
        lineas += [f"{clave.capitalize()}: {valor}" for clave, valor in datos.items() if str(valor).strip()]
        await self.chatwoot.send_message(token, acc, conv, "\n".join(lineas), private=True)
        self.result.interest_registered = True
        return "Interés de compra registrado. Recordá confirmarle al cliente cómo sigue con `responder`."


class Agent:
    def __init__(self, llm: anthropic.AsyncAnthropic, chatwoot: ChatwootClient, settings: Settings):
        self.llm = llm
        self.chatwoot = chatwoot
        self.settings = settings

    async def run(
        self,
        client: ClientConfig,
        ref: ConversationRef,
        messages: list[dict[str, Any]],
        *,
        contact_name: str | None = None,
        labels: list[str] | None = None,
    ) -> RunResult:
        actions = ConversationActions(self.chatwoot, client, ref)
        system = [
            # Parte estable (reglas + base de conocimiento): se cachea entre mensajes.
            {"type": "text", "text": build_system_prompt(client), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": build_turn_context(client, contact_name, labels or [], client.etiqueta_venta)},
        ]
        messages = list(messages)

        try:
            for _ in range(self.settings.max_agent_steps):
                response = await self._create(system, messages)

                if response.stop_reason == "refusal":
                    logger.warning("Claude rechazó responder (cuenta=%s conv=%s): %s",
                                   ref.account_id, ref.conversation_id, response.stop_details)
                    await actions.handoff("El asistente automático no pudo responder este mensaje.")
                    break

                content = _echoable(response.content)
                tool_uses = [b for b in content if b.type == "tool_use"]
                if not tool_uses:
                    await self._finish_without_tools(actions, response)
                    break

                messages.append({"role": "assistant", "content": content})
                results = [await self._execute(actions, block) for block in tool_uses]
                messages.append({"role": "user", "content": results})

                # Si ya le enviamos algo al cliente (respuesta o derivación), el turno terminó:
                # no hace falta otra llamada solo para que el modelo cierre.
                if actions.result.replies:
                    break
            else:
                logger.warning("Se agotaron los pasos del agente (cuenta=%s conv=%s)", ref.account_id, ref.conversation_id)
                if not actions.result.replies:
                    await actions.handoff("El asistente automático no logró resolver la consulta.")
        except HumanTookOver:
            actions.result.aborted_reason = "human_took_over"
            logger.info("Un humano tomó la conversación %s; el bot no responde", ref.conversation_id)
        return actions.result

    async def _create(self, system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        params: dict[str, Any] = {
            "model": self.settings.claude_model,
            "max_tokens": self.settings.claude_max_tokens,
            "system": system,
            "tools": TOOLS,
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.claude_effort},
            "messages": messages,
        }
        if self.settings.claude_fallbacks:
            # Si el clasificador de seguridad rechaza el pedido, la API lo reintenta en el
            # modelo de respaldo recomendado en lugar de devolver un rechazo.
            params["betas"] = [FALLBACKS_BETA]
            params["fallbacks"] = "default"
        return await self.llm.beta.messages.create(**params)

    async def _finish_without_tools(self, actions: ConversationActions, response: Any) -> None:
        if actions.result.replies:
            return
        texto = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if texto and response.stop_reason == "end_turn":
            # El modelo contestó en texto plano en lugar de usar `responder`: se lo enviamos igual.
            await actions.reply(texto)
        else:
            await actions.handoff("El asistente automático no generó una respuesta válida.")

    async def _execute(self, actions: ConversationActions, block: Any) -> dict[str, Any]:
        args = block.input if isinstance(block.input, dict) else {}
        try:
            if block.name == "responder":
                texto = _required_str(args, "texto")
                content = await actions.reply(texto)
            elif block.name == "derivar_a_humano":
                content = await actions.handoff(_required_str(args, "motivo"))
            elif block.name == "registrar_interes_de_compra":
                producto = _required_str(args, "producto")
                datos = args.get("datos") or {}
                if not isinstance(datos, dict):
                    raise ValueError("`datos` debe ser un objeto")
                content = await actions.register_interest(producto, datos)
            else:
                raise ValueError(f"Herramienta desconocida: {block.name}")
        except HumanTookOver:
            raise
        except ValueError as exc:
            return _tool_result(block.id, f"Parámetros inválidos: {exc}", error=True)
        except ChatwootError:
            logger.exception("Error ejecutando %s", block.name)
            return _tool_result(block.id, "Error de Chatwoot al ejecutar la acción.", error=True)
        logger.info("Herramienta %s ejecutada (conv=%s)", block.name, actions.ref.conversation_id)
        return _tool_result(block.id, content)


# --- Historial ---------------------------------------------------------------------

def build_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convierte mensajes de Chatwoot en turnos user/assistant para la API de Claude.

    Los mensajes del cliente pasan como `user`; los del negocio (bot o agentes humanos),
    como `assistant`. Turnos consecutivos del mismo rol se unen en uno solo.
    """
    turns: list[dict[str, Any]] = []
    for msg in history:
        if msg.get("private"):
            continue
        tipo = message_type_code(msg.get("message_type"))
        if tipo == INCOMING:
            role = "user"
        elif tipo in (OUTGOING, TEMPLATE):
            role = "assistant"
        else:
            continue
        text = _message_text(msg)
        if not text:
            continue
        sender_type = str((msg.get("sender") or {}).get("type", "")).lower()
        if role == "assistant" and sender_type == "user":
            text = f"[Mensaje de un agente humano] {text}"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})

    if turns and turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": "(La conversación la inició el negocio.)"})
    return turns


def _message_text(msg: dict[str, Any]) -> str:
    partes = []
    content = (msg.get("content") or "").strip()
    if content:
        partes.append(content)
    for att in msg.get("attachments") or []:
        partes.append(f"[Adjunto: {att.get('file_type', 'archivo')}]")
    return "\n".join(partes)


def _echoable(content: list[Any]) -> list[Any]:
    """Tras un fallback de modelo, los bloques internos previos al último marcador
    `fallback` no deben reenviarse a la API."""
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if boundary < 0:
        return content
    internos = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(content) if i > boundary or b.type not in internos]


def _required_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` es obligatorio y debe ser texto")
    return value.strip()


def _tool_result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        result["is_error"] = True
    return result
