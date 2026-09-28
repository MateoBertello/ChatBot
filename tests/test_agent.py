import pytest

from app.agent import Agent, ConversationRef, build_messages
from app.clients import ClientRegistry

from .conftest import ROOT, FakeChatwoot, FakeLLM, response, text, tool_use

REF = ConversationRef(account_id=1, conversation_id=7, bot_token="bot-token")
USER_MSG = [{"role": "user", "content": "Hola, ¿cuánto sale la torta rogel?"}]


@pytest.fixture
def client():
    return ClientRegistry(ROOT / "clients").get(1)


async def run(llm, chatwoot, client, settings, messages=USER_MSG, labels=None):
    return await Agent(llm, chatwoot, settings).run(client, REF, messages, contact_name="Ana", labels=labels)


async def test_responder_envia_mensaje_y_corta_el_loop(client, settings):
    llm = FakeLLM(response(tool_use("responder", texto="Sale $32.000 🎂")))
    cw = FakeChatwoot()
    result = await run(llm, cw, client, settings)

    assert cw.sent == [{"content": "Sale $32.000 🎂", "private": False}]
    assert result.replies == ["Sale $32.000 🎂"] and not result.handed_off
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in call["tools"]} == {"responder", "derivar_a_humano", "registrar_interes_de_compra"}


async def test_derivar_a_humano(client, settings):
    llm = FakeLLM(response(tool_use("derivar_a_humano", motivo="Reclamo por pedido")))
    cw = FakeChatwoot()
    result = await run(llm, cw, client, settings)

    assert result.handed_off
    assert cw.statuses == ["open"]
    assert cw.sent[0]["private"] and "Reclamo por pedido" in cw.sent[0]["content"]
    assert not cw.sent[1]["private"] and "equipo" in cw.sent[1]["content"]


async def test_registrar_interes_y_luego_responder(client, settings):
    llm = FakeLLM(
        response(tool_use("registrar_interes_de_compra", producto="Torta Rogel",
                          datos={"nombre": "Ana", "entrega": "retiro sábado"})),
        response(tool_use("responder", texto="¡Anotado! Te confirmamos la seña.")),
    )
    cw = FakeChatwoot()
    result = await run(llm, cw, client, settings)

    assert result.interest_registered
    assert cw.labels == ["venta"]
    nota = cw.sent[0]
    assert nota["private"] and "Torta Rogel" in nota["content"] and "Ana" in nota["content"]
    assert cw.sent[1] == {"content": "¡Anotado! Te confirmamos la seña.", "private": False}
    # El segundo llamado incluye el tool_result del primero.
    last = llm.calls[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"


async def test_parametros_invalidos_devuelven_error_al_modelo(client, settings):
    llm = FakeLLM(
        response(tool_use("responder", texto="")),
        response(tool_use("responder", texto="Hola!")),
    )
    cw = FakeChatwoot()
    await run(llm, cw, client, settings)
    error = llm.calls[1]["messages"][-1]["content"][0]
    assert error["is_error"] is True
    assert cw.sent == [{"content": "Hola!", "private": False}]


async def test_rechazo_del_modelo_deriva(client, settings):
    llm = FakeLLM(response(stop_reason="refusal"))
    cw = FakeChatwoot()
    result = await run(llm, cw, client, settings)
    assert result.handed_off and cw.statuses == ["open"]


async def test_texto_sin_herramienta_se_envia(client, settings):
    llm = FakeLLM(response(text("¡Hola! ¿En qué te ayudo?"), stop_reason="end_turn"))
    cw = FakeChatwoot()
    await run(llm, cw, client, settings)
    assert cw.sent == [{"content": "¡Hola! ¿En qué te ayudo?", "private": False}]


async def test_no_responde_si_un_humano_tomo_la_conversacion(client, settings):
    llm = FakeLLM(response(tool_use("responder", texto="Hola")))
    cw = FakeChatwoot(status="open")
    result = await run(llm, cw, client, settings)
    assert cw.sent == [] and result.aborted_reason == "human_took_over"


def test_build_messages():
    history = [
        {"id": 1, "message_type": 1, "content": "¡Bienvenido!", "sender": {"type": "agent_bot"}},
        {"id": 2, "message_type": 0, "content": "Hola"},
        {"id": 3, "message_type": "incoming", "content": "", "attachments": [{"file_type": "image"}]},
        {"id": 4, "message_type": 1, "content": "nota interna", "private": True},
        {"id": 5, "message_type": 2, "content": "Conversación asignada"},
        {"id": 6, "message_type": 1, "content": "Te ayudo yo", "sender": {"type": "user"}},
        {"id": 7, "message_type": 0, "content": "Gracias"},
    ]
    assert build_messages(history) == [
        {"role": "user", "content": "(La conversación la inició el negocio.)"},
        {"role": "assistant", "content": "¡Bienvenido!"},
        {"role": "user", "content": "Hola\n[Adjunto: image]"},
        {"role": "assistant", "content": "[Mensaje de un agente humano] Te ayudo yo"},
        {"role": "user", "content": "Gracias"},
    ]
