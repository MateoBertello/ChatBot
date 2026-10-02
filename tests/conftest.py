from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


def make_settings(**overrides: Any) -> Settings:
    base = dict(
        anthropic_api_key="test",
        chatwoot_url="https://chatwoot.test",
        chatwoot_bot_token="bot-token",
        webhook_secret="s3cret",
        chatwoot_api_token="",
        clients_dir=ROOT / "clients",
        claude_model="claude-opus-5",
        claude_effort="medium",
        claude_max_tokens=16000,
        claude_fallbacks=True,
        history_limit=20,
        debounce_seconds=0,
        max_agent_steps=6,
        log_level="INFO",
        transcription_provider="none",
        transcription_api_key="",
        transcription_model="",
        transcription_base_url="",
        transcription_language="es",
        media_max_per_run=4,
        media_allowed_hosts=(),
        usage_file=None,
        admin_token="",
        rate_limit_conversation_per_hour=20,
        rate_limit_account_per_day=1000,
    )
    base.update(overrides)
    return Settings(**base)


class FakeChatwoot:
    def __init__(self, status: str = "pending", messages: list[dict] | None = None, labels: list[str] | None = None):
        self.status = status
        self.messages = messages or []
        self.labels = labels or []
        self.sent: list[dict] = []
        self.statuses: list[str] = []
        self.list_error: Exception | None = None

    async def get_conversation(self, token, account_id, conversation_id):
        return {"id": conversation_id, "status": self.status, "labels": list(self.labels)}

    async def list_messages(self, token, account_id, conversation_id):
        if self.list_error:
            raise self.list_error
        return list(self.messages)

    async def send_message(self, token, account_id, conversation_id, content, *, private=False):
        self.sent.append({"content": content, "private": private})
        return {"id": len(self.sent)}

    async def set_status(self, token, account_id, conversation_id, status):
        self.statuses.append(status)
        self.status = status
        return {}

    async def add_labels(self, token, account_id, conversation_id, labels):
        for label in labels:
            if label not in self.labels:
                self.labels.append(label)
        return self.labels


def tool_use(name: str, **inputs: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{name}", name=name, input=inputs)


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def response(
    *blocks: SimpleNamespace, stop_reason: str = "tool_use", stop_details: Any = None, usage: Any = None
) -> SimpleNamespace:
    usage = usage or SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=4000
    )
    return SimpleNamespace(
        content=list(blocks), stop_reason=stop_reason, stop_details=stop_details, usage=usage, model="claude-opus-5"
    )


class FakeLLM:
    """Imita `AsyncAnthropic` devolviendo respuestas predefinidas en orden."""

    def __init__(self, *responses: SimpleNamespace):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> SimpleNamespace:
        self.calls.append({**params, "messages": list(params["messages"])})
        return self._responses.pop(0)


@pytest.fixture
def settings() -> Settings:
    return make_settings()
