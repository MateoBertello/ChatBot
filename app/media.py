"""Convierte los adjuntos de WhatsApp en contenido que Claude puede usar.

- Notas de voz: se transcriben (o se usa la transcripción de Chatwoot si ya existe).
- Imágenes y PDFs: se descargan y se envían a Claude como bloques `image` / `document`.
- Ubicaciones, contactos, videos, etc.: se describen en texto.

Solo se descargan los adjuntos de los mensajes que el bot está por responder; los
anteriores se mencionan en texto para no pagar tokens de imagen en cada turno.
"""

from __future__ import annotations

import base64
import logging
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import httpx

from .chatwoot import INCOMING, OUTGOING, TEMPLATE, message_type_code
from .transcription import MAX_AUDIO_BYTES, Transcriber, TranscriptionError
from .usage import UsageTracker

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # límite de la API de Claude por imagen
MAX_PDF_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
Block = dict[str, Any]


class MediaDownloadError(Exception):
    pass


def pending_incoming_ids(history: list[dict[str, Any]]) -> set[int]:
    """Ids de los mensajes del cliente posteriores a la última respuesta del negocio."""
    pending: set[int] = set()
    for msg in history:
        if msg.get("private"):
            continue
        tipo = message_type_code(msg.get("message_type"))
        if tipo in (OUTGOING, TEMPLATE):
            pending.clear()
        elif tipo == INCOMING:
            pending.add(int(msg.get("id", 0)))
    return pending


def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


class MediaProcessor:
    def __init__(
        self,
        http: httpx.AsyncClient,
        allowed_hosts: tuple[str, ...],
        transcriber: Transcriber | None = None,
        usage: UsageTracker | None = None,
        max_media_per_run: int = 4,
    ):
        self._http = http
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.transcriber = transcriber
        self.usage = usage
        self.max_media_per_run = max_media_per_run
        self._transcripts: OrderedDict[int, str] = OrderedDict()

    async def render(
        self, history: list[dict[str, Any]], account_id: int, conversation_id: int
    ) -> dict[int, list[Block]]:
        """Devuelve, por id de mensaje, los bloques que representan sus adjuntos."""
        pending = pending_incoming_ids(history)
        budget = self.max_media_per_run
        rendered: dict[int, list[Block]] = {}
        for msg in history:
            attachments = msg.get("attachments") or []
            if not attachments or msg.get("private"):
                continue
            msg_id = int(msg.get("id", 0))
            is_pending = msg_id in pending
            blocks: list[Block] = []
            for att in attachments:
                att_blocks, used_budget = await self._render_attachment(
                    att, is_pending, budget, account_id, conversation_id
                )
                budget -= used_budget
                blocks.extend(att_blocks)
            rendered[msg_id] = blocks
        return rendered

    async def _render_attachment(
        self, att: dict[str, Any], is_pending: bool, budget: int, account_id: int, conversation_id: int
    ) -> tuple[list[Block], int]:
        """Devuelve los bloques del adjunto y cuántas imágenes/PDFs consumió del cupo."""
        kind = str(att.get("file_type") or "archivo").lower()
        download = is_pending and budget > 0
        try:
            if kind == "audio":
                return [text_block(await self._audio_text(att, is_pending, account_id, conversation_id))], 0
            if kind == "image":
                if download:
                    return await self._image_blocks(att), 1
                return [text_block("[El cliente envió una imagen]")], 0
            if kind == "file" and _is_pdf(att):
                if download:
                    return await self._pdf_blocks(att), 1
                return [text_block("[El cliente envió un PDF]")], 0
        except (MediaDownloadError, TranscriptionError) as exc:
            logger.warning("No se pudo procesar un adjunto %s (conv %s): %s", kind, conversation_id, exc)
            return [text_block(f"[El cliente envió un adjunto ({kind}) que no se pudo procesar]")], 0
        return [text_block(_describe(att, kind))], 0

    # --- Audio ------------------------------------------------------------------

    async def _audio_text(self, att: dict[str, Any], download: bool, account_id: int, conversation_id: int) -> str:
        att_id = int(att.get("id", 0))
        transcript = (att.get("transcribed_text") or "").strip() or self._transcripts.get(att_id)
        if not transcript and download and self.transcriber is not None:
            data, content_type = await self._download(att.get("data_url", ""), MAX_AUDIO_BYTES)
            ext = (att.get("extension") or "ogg").lower()
            ext = "ogg" if ext in ("oga", "opus") else ext  # notas de voz de WhatsApp (Opus en OGG)
            transcript = await self.transcriber.transcribe(data, f"audio.{ext}", att.get("content_type") or content_type)
            self._remember(att_id, transcript)
            if self.usage:
                await self.usage.record(
                    "transcription", account_id, conversation_id,
                    provider=self.transcriber.provider, model=self.transcriber.model, bytes=len(data),
                )
        if transcript:
            return f"[Nota de voz, transcripción automática]: {transcript}"
        if download and self.transcriber is None:
            return "[El cliente envió una nota de voz y no hay transcripción disponible]"
        return "[El cliente envió una nota de voz]"

    def _remember(self, att_id: int, transcript: str) -> None:
        self._transcripts[att_id] = transcript
        if len(self._transcripts) > 5000:
            self._transcripts.popitem(last=False)

    # --- Imágenes y PDFs ----------------------------------------------------------

    async def _image_blocks(self, att: dict[str, Any]) -> list[Block]:
        data, content_type = await self._download(att.get("data_url", ""), MAX_IMAGE_BYTES)
        media_type = (att.get("content_type") or content_type).split(";")[0].strip().lower()
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in IMAGE_TYPES:
            raise MediaDownloadError(f"formato de imagen no soportado: {media_type}")
        return [
            text_block("[El cliente envió esta imagen]"),
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _b64(data)}},
        ]

    async def _pdf_blocks(self, att: dict[str, Any]) -> list[Block]:
        data, _ = await self._download(att.get("data_url", ""), MAX_PDF_BYTES)
        if not data.startswith(b"%PDF"):
            raise MediaDownloadError("el archivo no es un PDF válido")
        return [
            text_block("[El cliente envió este PDF]"),
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)}},
        ]

    # --- Descarga -----------------------------------------------------------------

    async def _download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in self.allowed_hosts:
            # Evita que una URL arbitraria haga que el bot descargue de cualquier lado.
            raise MediaDownloadError(f"host no permitido para descargar adjuntos: {parsed.hostname!r}")
        try:
            async with self._http.stream("GET", url, follow_redirects=True, timeout=30.0) as response:
                if response.status_code >= 400:
                    raise MediaDownloadError(f"HTTP {response.status_code}")
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise MediaDownloadError(f"archivo demasiado grande ({declared} bytes)")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaDownloadError("archivo demasiado grande")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise MediaDownloadError(repr(exc)) from exc


def _is_pdf(att: dict[str, Any]) -> bool:
    return (att.get("extension") or "").lower() == "pdf" or (att.get("content_type") or "").startswith("application/pdf")


def _describe(att: dict[str, Any], kind: str) -> str:
    if kind == "location":
        lat, lng = att.get("coordinates_lat"), att.get("coordinates_long")
        title = f"{att['fallback_title']} " if att.get("fallback_title") else ""
        return f"[El cliente compartió una ubicación: {title}({lat}, {lng})]"
    if kind == "contact":
        return f"[El cliente compartió un contacto: {att.get('fallback_title') or 'sin nombre'}]"
    if kind == "video":
        return "[El cliente envió un video (no se puede ver)]"
    if kind == "file":
        return f"[El cliente envió un archivo .{att.get('extension') or '?'} (no se puede abrir)]"
    return f"[El cliente envió un adjunto: {kind}]"


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
