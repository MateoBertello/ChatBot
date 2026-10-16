import pytest
from fastapi.testclient import TestClient

from app.simulador.__main__ import cargar_env
from app.simulador.web import create_sim_app

from .conftest import FakeLLM, make_settings, response, text, tool_use

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


@pytest.fixture
def sim():
    def _make(*responses):
        llm = FakeLLM(*responses)
        client = TestClient(create_sim_app(make_settings(debounce_seconds=0), llm=llm))
        client.__enter__()
        conv = client.post("/sim/api/conversaciones", data={"account_id": 1, "contacto": "Ana"}).json()
        base = f"/sim/api/conversaciones/1/{conv['id']}"
        return client, llm, base
    return _make


def enviar(client, base, texto="", **kwargs):
    return client.post(f"{base}/mensajes?esperar=true", data={"texto": texto}, **kwargs).json()


def test_info_lista_clientes(sim):
    client, _, _ = sim()
    info = client.get("/sim/api/info").json()
    assert {"account_id": 1, "nombre": "Panadería La Espiga"} in info["clientes"]
    assert client.get("/").status_code == 200


def test_conversacion_completa(sim):
    client, llm, base = sim(response(tool_use("responder", texto="¡Hola Ana! La rogel sale $32.000.")))
    assert enviar(client, base, "¿Cuánto sale la rogel?") == {"status": "accepted"}
    estado = client.get(base).json()
    assert [m["content"] for m in estado["mensajes"]] == ["¿Cuánto sale la rogel?", "¡Hola Ana! La rogel sale $32.000."]
    assert estado["mensajes"][1]["sender"]["type"] == "agent_bot"
    assert estado["consumo"]["llamadas_claude"] == 1 and estado["consumo"]["costo_estimado_usd"] > 0
    assert not estado["procesando"]
    assert "Ana" in llm.calls[0]["system"][1]["text"]


def test_derivar_y_luego_el_bot_no_responde(sim):
    client, llm, base = sim(response(tool_use("derivar_a_humano", motivo="Pide hablar con una persona")))
    enviar(client, base, "Quiero hablar con una persona")
    estado = client.get(base).json()
    assert estado["status"] == "open"
    assert any(m["private"] and "Pide hablar" in m["content"] for m in estado["mensajes"])

    r = enviar(client, base, "¿Hola?")
    assert r["status"] == "ignored" and "Abierta" in r["motivo"]
    assert len(llm.calls) == 1


def test_resolver_y_el_bot_retoma(sim):
    client, _, base = sim(response(tool_use("responder", texto="¡Buenas!")))
    client.post(f"{base}/estado", data={"status": "resolved"})
    enviar(client, base, "Hola de nuevo")
    estado = client.get(base).json()
    assert estado["status"] == "pending"
    assert estado["mensajes"][-1]["content"] == "¡Buenas!"


def test_venta_agrega_etiqueta_y_nota(sim):
    client, _, base = sim(
        response(tool_use("registrar_interes_de_compra", producto="Chocotorta", datos={"nombre": "Ana"})),
        response(tool_use("responder", texto="¡Anotado!")),
    )
    enviar(client, base, "Quiero encargar una chocotorta")
    estado = client.get(base).json()
    assert estado["labels"] == ["venta"]
    assert any(m["private"] and "Chocotorta" in m["content"] for m in estado["mensajes"])


def test_imagen_llega_a_claude(sim):
    client, llm, base = sim(response(tool_use("responder", texto="¡Linda!")))
    enviar(client, base, "¿Tienen esta?", files={"archivo": ("torta.png", PNG, "image/png")})
    content = llm.calls[0]["messages"][-1]["content"]
    assert [b["type"] for b in content] == ["text", "image"]
    estado = client.get(base).json()
    url = estado["mensajes"][0]["attachments"][0]["data_url"].replace("http://simulador.local", "")
    assert client.get(url).content == PNG


def test_respuesta_del_agente_entra_al_historial(sim):
    client, llm, base = sim(response(text("Perfecto"), stop_reason="end_turn"))
    client.post(f"{base}/agente", data={"texto": "Hola, soy Juan del local"})
    enviar(client, base, "Gracias Juan")
    roles = [(m["role"], m["content"]) for m in llm.calls[0]["messages"]]
    assert ("assistant", "[Mensaje de un agente humano] Hola, soy Juan del local") in roles


def test_validaciones(sim):
    client, _, base = sim()
    assert client.post(f"{base}/mensajes", data={"texto": "  "}).status_code == 400
    assert client.post("/sim/api/conversaciones", data={"account_id": 999}).status_code == 404
    assert client.get("/sim/api/conversaciones/1/999").status_code == 404
    assert client.post(f"{base}/estado", data={"status": "otro"}).status_code == 400


def test_cargar_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comentario\nSIM_A=uno\nSIM_B="dos"  # nota\nSIM_C=\nSIM_EXISTE=nuevo\n')
    monkeypatch.setenv("SIM_EXISTE", "viejo")
    for k in ("SIM_A", "SIM_B", "SIM_C"):
        monkeypatch.delenv(k, raising=False)
    cargar_env(env)
    import os
    assert os.environ["SIM_A"] == "uno" and os.environ["SIM_B"] == "dos"
    assert "SIM_C" not in os.environ and os.environ["SIM_EXISTE"] == "viejo"
    for k in ("SIM_A", "SIM_B"):
        monkeypatch.delenv(k)
