"""Chatwoot en memoria con la misma interfaz que `ChatwootClient`.

El bot real (servicio, agente, adjuntos, límites) corre sin cambios contra esta clase;
solo cambia a dónde van los mensajes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..chatwoot import ACTIVITY, INCOMING, OUTGOING, ChatwootError

ESTADOS = ("pending", "open", "resolved")


@dataclass
class Archivo:
    contenido: bytes
    content_type: str
    nombre: str


@dataclass
class Conversacion:
    account_id: int
    id: int
    contacto: str
    status: str = "pending"
    labels: list[str] = field(default_factory=list)
    mensajes: list[dict[str, Any]] = field(default_factory=list)


class ChatwootSimulado:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.conversaciones: dict[tuple[int, int], Conversacion] = {}
        self.archivos: dict[int, Archivo] = {}
        self._ultimo_mensaje = 0
        self._ultimo_adjunto = 0
        self._ultima_conversacion: dict[int, int] = {}

    # --- Acciones del simulador (cliente y agente humano) -------------------------

    def nueva_conversacion(self, account_id: int, contacto: str) -> Conversacion:
        conv_id = self._ultima_conversacion.get(account_id, 0) + 1
        self._ultima_conversacion[account_id] = conv_id
        conv = Conversacion(account_id, conv_id, contacto)
        self.conversaciones[(account_id, conv_id)] = conv
        return conv

    def conversacion(self, account_id: int, conversation_id: int) -> Conversacion:
        conv = self.conversaciones.get((account_id, conversation_id))
        if conv is None:
            raise ChatwootError(f"No existe la conversación {conversation_id} en la cuenta {account_id}", 404)
        return conv

    def mensaje_del_cliente(self, conv: Conversacion, texto: str, archivo: Archivo | None = None) -> dict[str, Any]:
        """Agrega un mensaje entrante y devuelve el payload de webhook que mandaría Chatwoot."""
        # Igual que Chatwoot con un bot conectado: si estaba resuelta, vuelve a pendiente.
        if conv.status == "resolved":
            self._cambiar_estado(conv, "pending", "La conversación se reabrió: el cliente volvió a escribir")
        adjuntos = [self._guardar_adjunto(archivo)] if archivo else []
        mensaje = self._agregar(conv, texto, INCOMING, sender={"name": conv.contacto, "type": "contact"},
                                attachments=adjuntos)
        return {
            "event": "message_created",
            "id": mensaje["id"],
            "content": texto,
            "message_type": "incoming",
            "private": False,
            "account": {"id": conv.account_id},
            "conversation": {"id": conv.id, "status": conv.status, "labels": list(conv.labels)},
            "sender": {"name": conv.contacto, "type": "contact"},
            "attachments": adjuntos,
        }

    def mensaje_del_agente(self, conv: Conversacion, texto: str) -> dict[str, Any]:
        return self._agregar(conv, texto, OUTGOING, sender={"name": "Agente humano", "type": "user"})

    def cambiar_estado(self, conv: Conversacion, status: str) -> None:
        if status not in ESTADOS:
            raise ValueError(f"Estado inválido: {status}")
        textos = {"open": "Un agente tomó la conversación", "resolved": "Conversación resuelta",
                  "pending": "Conversación devuelta al bot"}
        self._cambiar_estado(conv, status, textos[status])

    # --- Interfaz de ChatwootClient (la usa el bot) ---------------------------------

    async def get_conversation(self, token: str, account_id: int, conversation_id: int) -> dict[str, Any]:
        conv = self.conversacion(account_id, conversation_id)
        return {"id": conv.id, "status": conv.status, "labels": list(conv.labels)}

    async def list_messages(self, token: str, account_id: int, conversation_id: int) -> list[dict[str, Any]]:
        conv = self.conversacion(account_id, conversation_id)
        # Igual que `filter_internal_messages`: sin notas privadas ni actividad, últimos 20.
        visibles = [m for m in conv.mensajes if not m["private"] and m["message_type"] != ACTIVITY]
        return [dict(m) for m in visibles[-20:]]

    async def send_message(
        self, token: str, account_id: int, conversation_id: int, content: str, *, private: bool = False
    ) -> dict[str, Any]:
        conv = self.conversacion(account_id, conversation_id)
        return self._agregar(conv, content, OUTGOING, sender={"name": "Bot", "type": "agent_bot"}, private=private)

    async def set_status(self, token: str, account_id: int, conversation_id: int, status: str) -> dict[str, Any]:
        conv = self.conversacion(account_id, conversation_id)
        if status != conv.status:
            self._cambiar_estado(conv, status, "El bot derivó la conversación a una persona"
                                 if status == "open" else f"Estado cambiado a {status}")
        return {"current_status": conv.status}

    async def add_labels(self, token: str, account_id: int, conversation_id: int, labels: list[str]) -> list[str]:
        conv = self.conversacion(account_id, conversation_id)
        for label in labels:
            if label not in conv.labels:
                conv.labels.append(label)
                self._agregar(conv, f"Se agregó la etiqueta «{label}»", ACTIVITY, sender=None)
        return list(conv.labels)

    async def aclose(self) -> None:
        return None

    # --- Interno ------------------------------------------------------------------------

    def _agregar(
        self,
        conv: Conversacion,
        texto: str,
        tipo: int,
        *,
        sender: dict[str, Any] | None,
        private: bool = False,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._ultimo_mensaje += 1
        mensaje: dict[str, Any] = {
            "id": self._ultimo_mensaje,
            "content": texto,
            "message_type": tipo,
            "private": private,
            "created_at": int(time.time()),
            "sender": sender,
        }
        if attachments:
            mensaje["attachments"] = attachments
        conv.mensajes.append(mensaje)
        return mensaje

    def _cambiar_estado(self, conv: Conversacion, status: str, texto: str) -> None:
        conv.status = status
        self._agregar(conv, texto, ACTIVITY, sender=None)

    def _guardar_adjunto(self, archivo: Archivo) -> dict[str, Any]:
        self._ultimo_adjunto += 1
        self.archivos[self._ultimo_adjunto] = archivo
        ctype = archivo.content_type.split(";")[0].strip().lower()
        if ctype.startswith("image/"):
            tipo = "image"
        elif ctype.startswith("audio/"):
            tipo = "audio"
        else:
            tipo = "file"
        extension = archivo.nombre.rsplit(".", 1)[-1].lower() if "." in archivo.nombre else ""
        return {
            "id": self._ultimo_adjunto,
            "file_type": tipo,
            "extension": extension,
            "content_type": ctype,
            "file_size": len(archivo.contenido),
            "data_url": f"{self.base_url}/sim/archivos/{self._ultimo_adjunto}",
        }
