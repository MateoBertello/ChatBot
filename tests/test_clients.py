from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.clients import AtencionHumana, ClientNotFound, ClientRegistry
from app.prompts import build_system_prompt, build_turn_context

from .conftest import ROOT

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_carga_cliente_de_ejemplo():
    client = ClientRegistry(ROOT / "clients").get(1)
    assert client.negocio.nombre == "Panadería La Espiga"
    assert client.base_conocimiento.productos
    prompt = build_system_prompt(client)
    assert "Torta Rogel" in prompt and "32000" in prompt
    assert "derivar_a_humano" in prompt


@pytest.mark.parametrize("account_id", [999, "../etc/passwd", None])
def test_cliente_inexistente_o_invalido(account_id):
    with pytest.raises(ClientNotFound):
        ClientRegistry(ROOT / "clients").get(account_id)


def test_disponibilidad_humana():
    atencion = AtencionHumana(
        zona_horaria="America/Argentina/Buenos_Aires",
        horario={"Lunes": ["09:00-13:00"], "sábado": ["22:00-02:00"]},
    )
    assert atencion.disponible(datetime(2026, 9, 28, 10, 0, tzinfo=TZ))  # lunes
    assert not atencion.disponible(datetime(2026, 9, 28, 13, 0, tzinfo=TZ))
    assert not atencion.disponible(datetime(2026, 9, 29, 10, 0, tzinfo=TZ))  # martes
    assert atencion.disponible(datetime(2026, 10, 3, 23, 30, tzinfo=TZ))  # sábado de noche


def test_horario_invalido():
    with pytest.raises(ValueError):
        AtencionHumana(horario={"lunes": ["25:00-26:00"]})
    with pytest.raises(ValueError):
        AtencionHumana(horario={"funday": ["09:00-10:00"]})


def test_contexto_del_turno():
    client = ClientRegistry(ROOT / "clients").get(1)
    ctx = build_turn_context(client, "Ana", ["venta"], "venta", datetime(2026, 9, 28, 10, 0, tzinfo=TZ))
    assert "lunes 28/09/2026 10:00" in ctx
    assert "disponible ahora" in ctx
    assert "Ana" in ctx
    assert "Ya se registró" in ctx
