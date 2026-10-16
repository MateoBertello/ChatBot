"""Configuración del servicio a partir de variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria {name}")
    return value or ""


OPENROUTER_BASE_URL = "https://openrouter.ai/api"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    # Gateway opcional compatible con la API de mensajes de Anthropic (p. ej. OpenRouter).
    # Vacío = API de Anthropic directa.
    llm_base_url: str
    llm_auth_token: str
    chatwoot_url: str
    chatwoot_bot_token: str
    webhook_secret: str
    # Token opcional de un usuario/agente de Chatwoot. Los tokens de Agent Bot no
    # tienen permiso para listar mensajes; si se define, se usa para traer el
    # historial de la conversación.
    chatwoot_api_token: str
    clients_dir: Path
    claude_model: str
    claude_effort: str  # vacío = no se envía (el modelo usa su valor por defecto)
    claude_thinking: str  # "adaptive" o vacío (no se envía)
    claude_max_tokens: int
    claude_fallbacks: bool
    history_limit: int
    debounce_seconds: float
    max_agent_steps: int
    log_level: str
    # Transcripción de notas de voz (API compatible con OpenAI: OpenAI, Groq, etc.)
    transcription_provider: str
    transcription_api_key: str
    transcription_model: str
    transcription_base_url: str
    transcription_language: str
    # Imágenes, PDFs y audios
    media_max_per_run: int
    media_allowed_hosts: tuple[str, ...]
    # Consumo y límites
    usage_file: Path | None
    admin_token: str
    rate_limit_conversation_per_hour: int
    rate_limit_account_per_day: int


@lru_cache
def get_settings() -> Settings:
    # Con OPENROUTER_API_KEY se usa OpenRouter, que expone la misma API de mensajes de
    # Anthropic. Por defecto no se envían parámetros que un gateway puede no aceptar
    # (fallbacks beta, effort, thinking); el modelo usa sus valores por defecto.
    openrouter_key = _env("OPENROUTER_API_KEY", "")
    gateway = bool(openrouter_key)
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY", required=not gateway),
        llm_base_url=_env("LLM_BASE_URL", OPENROUTER_BASE_URL if gateway else "").rstrip("/"),
        llm_auth_token=openrouter_key,
        chatwoot_url=_env("CHATWOOT_URL", required=True).rstrip("/"),
        chatwoot_bot_token=_env("CHATWOOT_BOT_TOKEN", required=True),
        webhook_secret=_env("WEBHOOK_SECRET", required=True),
        chatwoot_api_token=_env("CHATWOOT_API_TOKEN", ""),
        clients_dir=Path(_env("CLIENTS_DIR", "clients")),
        claude_model=_env("CLAUDE_MODEL", "anthropic/claude-opus-5" if gateway else "claude-opus-5"),
        claude_effort=_env("CLAUDE_EFFORT", "" if gateway else "medium").lower(),
        claude_thinking=_env("CLAUDE_THINKING", "" if gateway else "adaptive").lower(),
        claude_max_tokens=int(_env("CLAUDE_MAX_TOKENS", "16000")),
        claude_fallbacks=_env("CLAUDE_FALLBACKS", "false" if gateway else "true").lower() in ("1", "true", "yes"),
        history_limit=int(_env("HISTORY_LIMIT", "20")),
        debounce_seconds=float(_env("DEBOUNCE_SECONDS", "2")),
        max_agent_steps=int(_env("MAX_AGENT_STEPS", "6")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        transcription_provider=_env("TRANSCRIPTION_PROVIDER", "none").lower(),
        transcription_api_key=_env("TRANSCRIPTION_API_KEY", ""),
        transcription_model=_env("TRANSCRIPTION_MODEL", ""),
        transcription_base_url=_env("TRANSCRIPTION_BASE_URL", ""),
        transcription_language=_env("TRANSCRIPTION_LANGUAGE", "es"),
        media_max_per_run=int(_env("MEDIA_MAX_PER_RUN", "4")),
        media_allowed_hosts=tuple(h.strip().lower() for h in _env("MEDIA_ALLOWED_HOSTS", "").split(",") if h.strip()),
        usage_file=Path(_env("USAGE_FILE", "data/usage.jsonl")) if _env("USAGE_FILE", "data/usage.jsonl") else None,
        admin_token=_env("ADMIN_TOKEN", ""),
        rate_limit_conversation_per_hour=int(_env("RATE_LIMIT_CONVERSATION_PER_HOUR", "20")),
        rate_limit_account_per_day=int(_env("RATE_LIMIT_ACCOUNT_PER_DAY", "1000")),
    )
