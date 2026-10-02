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


def test_limite_por_conversacion_deriva(settings):
    from .conftest import make_settings

    settings = make_settings(rate_limit_conversation_per_hour=1)
    client, _, cw, llm = make(settings)
    client.post("/webhook?token=s3cret", json=incoming(message_id=100))
    cw.status = "pending"  # el bot respondió; sigue pendiente
    client.post("/webhook?token=s3cret", json=incoming(message_id=101))
    assert len(llm.calls) == 1
    assert cw.statuses == ["open"]
    assert any(m["private"] and "límite" in m["content"] for m in cw.sent)


def test_limite_por_cuenta_en_yaml(settings, tmp_path):
    import shutil

    from .conftest import make_settings

    shutil.copy(ROOT / "clients" / "1.yaml", tmp_path / "1.yaml")
    with (tmp_path / "1.yaml").open("a") as fh:
        fh.write("\nlimites:\n  respuestas_por_dia: 0\n  respuestas_por_conversacion_por_hora: 0\n")
    settings = make_settings(clients_dir=tmp_path, rate_limit_conversation_per_hour=1)
    cw = FakeChatwoot()
    llm = FakeLLM(*[response(tool_use("responder", texto="ok")) for _ in range(3)])
    service = BotService(settings, ClientRegistry(tmp_path), cw, Agent(llm, cw, settings))
    client = TestClient(create_app(service))
    for message_id in (100, 101, 102):
        client.post("/webhook?token=s3cret", json=incoming(message_id=message_id))
    assert len(llm.calls) == 3 and not cw.statuses  # 0 = sin límite, pisa el valor del entorno


def test_registra_consumo_y_endpoint_usage(tmp_path):
    from app.usage import UsageTracker

    from .conftest import make_settings

    settings = make_settings(admin_token="admin", usage_file=tmp_path / "u.jsonl")
    cw = FakeChatwoot()
    llm = FakeLLM(response(tool_use("responder", texto="Sí, hacemos envíos.")))
    usage = UsageTracker(settings.usage_file)
    service = BotService(settings, ClientRegistry(ROOT / "clients"), cw,
                         Agent(llm, cw, settings, usage=usage), usage=usage)
    client = TestClient(create_app(service))
    client.post("/webhook?token=s3cret", json=incoming())

    assert client.get("/usage").status_code == 401
    assert client.get("/usage?token=otro").status_code == 401
    assert client.get("/usage?mes=octubre", headers={"Authorization": "Bearer admin"}).status_code == 400
    data = client.get("/usage", headers={"Authorization": "Bearer admin"}).json()
    acc = data["accounts"]["1"]
    assert acc["mensajes_atendidos"] == 1 and acc["llamadas_claude"] == 1
    assert acc["costo_estimado_usd"] == 0.0095


def test_usage_deshabilitado_sin_admin_token(settings):
    client, *_ = make(settings)
    assert client.get("/usage?token=").status_code == 404


def test_imagen_del_webhook_llega_a_claude(settings):
    import httpx

    from app.media import MediaProcessor

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=png, headers={"content-type": "image/png"})))
    cw = FakeChatwoot()
    llm = FakeLLM(response(tool_use("responder", texto="¡Esa la tenemos!")))
    service = BotService(settings, ClientRegistry(ROOT / "clients"), cw, Agent(llm, cw, settings),
                         media=MediaProcessor(http, ("chatwoot.test",)))
    client = TestClient(create_app(service))
    payload = incoming(content="¿Tienen esta?", attachments=[
        {"id": 3, "file_type": "image", "data_url": "https://chatwoot.test/rails/active_storage/blobs/x"}])
    client.post("/webhook?token=s3cret", json=payload)
    content = llm.calls[0]["messages"][-1]["content"]
    assert [b["type"] for b in content] == ["text", "image"]
    assert content[0]["text"] == "¿Tienen esta?\n[El cliente envió esta imagen]"
