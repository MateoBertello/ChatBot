import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient

from app.agent import Agent, ConversationRef
from app.clients import ClientRegistry
from app.config import OPENROUTER_BASE_URL, get_settings
from app.llm import build_llm, provider_name
from app.usage import estimate_cost, normalize_model

from .conftest import ROOT, FakeChatwoot, FakeLLM, make_settings, response, tool_use


@pytest.fixture
def entorno(monkeypatch):
    for var in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_THINKING", "CLAUDE_FALLBACKS", "LLM_BASE_URL",
                "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHATWOOT_URL", "https://cw.test")
    monkeypatch.setenv("CHATWOOT_BOT_TOKEN", "t")
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_config_openrouter(entorno):
    entorno.setenv("OPENROUTER_API_KEY", "sk-or-123")
    entorno.setenv("CLAUDE_MODEL", "anthropic/claude-haiku-5.5")
    s = get_settings()
    assert s.anthropic_api_key == "" and s.llm_auth_token == "sk-or-123"
    assert s.llm_base_url == OPENROUTER_BASE_URL
    assert s.claude_model == "anthropic/claude-haiku-5.5"
    assert (s.claude_effort, s.claude_thinking, s.claude_fallbacks) == ("", "", False)
    assert provider_name(s) == "OpenRouter"


def test_config_anthropic_directo(entorno):
    entorno.setenv("ANTHROPIC_API_KEY", "sk-ant")
    s = get_settings()
    assert s.llm_base_url == "" and s.claude_model == "claude-opus-5"
    assert (s.claude_effort, s.claude_thinking, s.claude_fallbacks) == ("medium", "adaptive", True)


def test_config_sin_ninguna_key(entorno):
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        get_settings()


async def test_pedido_a_openrouter(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-no-debe-viajar")
    pedidos = []

    def handler(request):
        pedidos.append(request)
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "anthropic/claude-haiku-5.5",
            "content": [{"type": "tool_use", "id": "t1", "name": "responder", "input": {"texto": "¡Hola!"}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}})

    settings = make_settings(llm_base_url=OPENROUTER_BASE_URL, llm_auth_token="sk-or-123",
                             claude_model="anthropic/claude-haiku-5.5", claude_effort="", claude_thinking="",
                             claude_fallbacks=False)
    llm = build_llm(settings)
    llm._client = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    cw = FakeChatwoot()
    client = ClientRegistry(ROOT / "clients").get(1)
    await Agent(llm, cw, settings).run(client, ConversationRef(1, 7, "t"), [{"role": "user", "content": "hola"}])

    req = pedidos[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/messages"
    assert req.headers["authorization"] == "Bearer sk-or-123"
    assert "x-api-key" not in req.headers and "anthropic-beta" not in req.headers
    body = __import__("json").loads(req.content)
    assert body["model"] == "anthropic/claude-haiku-5.5"
    assert not {"thinking", "output_config", "fallbacks"} & set(body)
    assert cw.sent == [{"content": "¡Hola!", "private": False}]


async def test_parametros_opcionales():
    llm = FakeLLM(response(tool_use("responder", texto="ok")))
    settings = make_settings(claude_effort="low", claude_thinking="adaptive", claude_fallbacks=False)
    client = ClientRegistry(ROOT / "clients").get(1)
    await Agent(llm, FakeChatwoot(), settings).run(client, ConversationRef(1, 7, "t"), [{"role": "user", "content": "x"}])
    call = llm.calls[0]
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "low"}
    assert "betas" not in call and "fallbacks" not in call


def test_precio_con_ids_de_openrouter():
    assert normalize_model("anthropic/claude-haiku-5.5:batch") == "claude-haiku-5-5"
    assert normalize_model("claude-opus-5") == "claude-opus-5"
    # 1000 de entrada y 100 de salida a 0,10 / 0,50 US$ por millón
    assert estimate_cost("anthropic/claude-haiku-5.5", 1000, 100, 0, 0) == 0.00015
