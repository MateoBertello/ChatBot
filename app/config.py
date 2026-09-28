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


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    chatwoot_url: str
    chatwoot_bot_token: str
    webhook_secret: str
    # Token opcional de un usuario/agente de Chatwoot. Los tokens de Agent Bot no
    # tienen permiso para listar mensajes; si se define, se usa para traer el
    # historial de la conversación.
    chatwoot_api_token: str
    clients_dir: Path
    claude_model: str
    claude_effort: str
    claude_max_tokens: int
    claude_fallbacks: bool
    history_limit: int
    debounce_seconds: float
    max_agent_steps: int
    log_level: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY", required=True),
        chatwoot_url=_env("CHATWOOT_URL", required=True).rstrip("/"),
        chatwoot_bot_token=_env("CHATWOOT_BOT_TOKEN", required=True),
        webhook_secret=_env("WEBHOOK_SECRET", required=True),
        chatwoot_api_token=_env("CHATWOOT_API_TOKEN", ""),
        clients_dir=Path(_env("CLIENTS_DIR", "clients")),
        claude_model=_env("CLAUDE_MODEL", "claude-opus-5"),
        claude_effort=_env("CLAUDE_EFFORT", "medium"),
        claude_max_tokens=int(_env("CLAUDE_MAX_TOKENS", "16000")),
        claude_fallbacks=_env("CLAUDE_FALLBACKS", "true").lower() in ("1", "true", "yes"),
        history_limit=int(_env("HISTORY_LIMIT", "20")),
        debounce_seconds=float(_env("DEBOUNCE_SECONDS", "2")),
        max_agent_steps=int(_env("MAX_AGENT_STEPS", "6")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
