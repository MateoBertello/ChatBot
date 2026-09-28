import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.chatwoot import ChatwootError
from app.clients import ClientRegistry
from app.main import create_app, verify_webhook
from app.service import BotService

from .conftest import ROOT, FakeChatwoot, FakeLLM, response, tool_use


def incoming(message_id=100, conversation_status="pending", account_id=1, **extra):
    payload = {
        "event": "message_created",
        "id": message_id,
        "content": "Hola, ¿hacen envíos?",
        "message_type": "incoming",
        "private": False,
        "account": {"id": account_id},
        "conversation": {"id": 7, "status": conversation_status},
        "sender": {"name": "Ana", "type": "contact"},
    }
    payload.update(extra)
    return payload


def make(settings, llm=None, chatwoot=None):
    chatwoot = chatwoot or FakeChatwoot()
    llm = llm or FakeLLM(response(tool_use("responder", texto="Sí, hacemos envíos.")))
    service = BotService(settings, ClientRegistry(ROOT / "clients"), chatwoot, Agent(llm, chatwoot, settings))
    return TestClient(create_app(service)), service, chatwoot, llm


def test_verify_webhook_variantes():
    secret, body = "s3cret", b'{"a":1}'
    assert verify_webhook({}, "s3cret", body, secret)
    assert verify_webhook({"x-webhook-token": "s3cret"}, None, body, secret)
    assert verify_webhook({"authorization": "Bearer s3cret"}, None, body, secret)
    assert not verify_webhook({}, "otro", body, secret)
    assert not verify_webhook({}, None, body, secret)

    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_webhook({"x-chatwoot-signature": sig, "x-chatwoot-timestamp": ts}, None, body, secret)
    assert not verify_webhook({"x-chatwoot-signature": sig, "x-chatwoot-timestamp": ts}, None, b"otro", secret)
    viejo = str(int(time.time()) - 3600)
    sig_viejo = "sha256=" + hmac.new(secret.encode(), f"{viejo}.".encode() + body, hashlib.sha256).hexdigest()
    assert not verify_webhook({"x-chatwoot-signature": sig_viejo, "x-chatwoot-timestamp": viejo}, None, body, secret)


def test_rechaza_sin_token(settings):
    client, *_ = make(settings)
    assert client.post("/webhook", json=incoming()).status_code == 401


def test_procesa_mensaje_entrante(settings):
    client, _, cw, llm = make(settings)
    r = client.post("/webhook?token=s3cret", json=incoming())
    assert r.json() == {"status": "accepted"}
    assert cw.sent == [{"content": "Sí, hacemos envíos.", "private": False}]
    # Sin historial disponible, se usa el mensaje del webhook.
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "Hola, ¿hacen envíos?"}]


def test_usa_historial_de_chatwoot(settings):
    history = [
        {"id": 90, "message_type": 0, "content": "Hola"},
        {"id": 91, "message_type": 1, "content": "¡Hola! ¿Qué necesitás?"},
        {"id": 100, "message_type": 0, "content": "Hola, ¿hacen envíos?"},
    ]
    client, _, _, llm = make(settings, chatwoot=FakeChatwoot(messages=history))
    client.post("/webhook?token=s3cret", json=incoming())
    assert [m["role"] for m in llm.calls[0]["messages"]] == ["user", "assistant", "user"]


def test_historial_no_disponible_usa_mensaje_actual(settings):
    cw = FakeChatwoot()
    cw.list_error = ChatwootError("HTTP 401", 401)
    client, _, cw, llm = make(settings, chatwoot=cw)
    client.post("/webhook?token=s3cret", json=incoming())
    assert len(llm.calls) == 1 and cw.sent


@pytest.mark.parametrize(
    "payload",
    [
        incoming(event="conversation_created"),
        incoming(message_type="outgoing"),
        incoming(private=True),
        incoming(conversation_status="open"),
        incoming(account_id=999),
    ],
)
def test_ignora_eventos_que_no_corresponden(settings, payload):
    client, _, cw, llm = make(settings)
    assert client.post("/webhook?token=s3cret", json=payload).json() == {"status": "ignored"}
    assert not llm.calls and not cw.sent


def test_ignora_duplicados(settings):
    client, *_ = make(settings)
    assert client.post("/webhook?token=s3cret", json=incoming()).json()["status"] == "accepted"
    assert client.post("/webhook?token=s3cret", json=incoming()).json()["status"] == "ignored"


def test_no_responde_si_humano_tomo_la_conversacion(settings):
    client, _, cw, llm = make(settings, chatwoot=FakeChatwoot(status="open"))
    client.post("/webhook?token=s3cret", json=incoming())
    assert not llm.calls and not cw.sent


def test_error_de_claude_deriva_a_humano(settings):
    class BoomLLM(FakeLLM):
        async def _create(self, **params):
            raise RuntimeError("API caída")

    client, _, cw, _ = make(settings, llm=BoomLLM())
    client.post("/webhook?token=s3cret", json=incoming())
    assert cw.statuses == ["open"]
    assert any(m["private"] and "Error técnico" in m["content"] for m in cw.sent)


async def test_debounce_responde_solo_al_ultimo(settings):
    import asyncio

    from .conftest import make_settings

    settings = make_settings(debounce_seconds=0.05)
    cw = FakeChatwoot()
    llm = FakeLLM(response(tool_use("responder", texto="Respuesta única")))
    service = BotService(settings, ClientRegistry(ROOT / "clients"), cw, Agent(llm, cw, settings))
    e1 = service.accept(incoming(message_id=100))
    e2 = service.accept(incoming(message_id=101, content="¿y a Colegiales?"))
    await asyncio.gather(service.process(e1), service.process(e2))
    assert len(llm.calls) == 1 and cw.sent == [{"content": "Respuesta única", "private": False}]
