"""Transcripción de notas de voz con una API compatible con OpenAI (`/audio/transcriptions`).

Sirve para OpenAI, Groq o cualquier servidor Whisper que exponga la misma API.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

# proveedor -> (URL base, modelo por defecto)
PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini-transcribe"),
    "groq": ("https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
}

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # límite de las APIs de transcripción


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str, language: str, http: httpx.AsyncClient):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.language = language
        self._http = http

    async def transcribe(self, data: bytes, filename: str, content_type: str) -> str:
        form = {"model": self.model, "response_format": "json"}
        if self.language:
            form["language"] = self.language
        try:
            response = await self._http.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=form,
                files={"file": (filename, data, content_type)},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise TranscriptionError(repr(exc)) from exc
        if response.status_code >= 400:
            raise TranscriptionError(f"HTTP {response.status_code}: {response.text[:300]}")
        text = response.json().get("text", "")
        return text.strip()


def build_transcriber(settings: Settings, http: httpx.AsyncClient) -> Transcriber | None:
    provider = settings.transcription_provider
    if provider in ("", "none", "off"):
        return None
    base_url, model = PRESETS.get(provider, ("", ""))
    base_url = settings.transcription_base_url or base_url
    model = settings.transcription_model or model
    if not base_url or not model:
        raise RuntimeError(
            f"TRANSCRIPTION_PROVIDER={provider!r} requiere TRANSCRIPTION_BASE_URL y TRANSCRIPTION_MODEL"
        )
    if not settings.transcription_api_key:
        raise RuntimeError("Falta TRANSCRIPTION_API_KEY para transcribir notas de voz")
    logger.info("Transcripción de audio activada: %s (%s)", provider, model)
    return Transcriber(provider, base_url, settings.transcription_api_key, model, settings.transcription_language, http)
