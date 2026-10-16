"""Cliente de la API de Claude: directo con Anthropic o a través de un gateway (OpenRouter)."""

from __future__ import annotations

import anthropic

from .config import Settings


def build_llm(settings: Settings) -> anthropic.AsyncAnthropic:
    if settings.llm_auth_token:
        # OpenRouter y otros gateways autentican con "Authorization: Bearer". Al pasar el
        # token explícito, el SDK no lee ANTHROPIC_API_KEY y no la envía al gateway.
        return anthropic.AsyncAnthropic(
            auth_token=settings.llm_auth_token,
            base_url=settings.llm_base_url or None,
            default_headers={"X-Title": "Chatwoot Agent Bot"},
        )
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, base_url=settings.llm_base_url or None)


def provider_name(settings: Settings) -> str:
    if not settings.llm_base_url:
        return "Anthropic"
    return "OpenRouter" if "openrouter.ai" in settings.llm_base_url else settings.llm_base_url
