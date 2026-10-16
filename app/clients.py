"""Carga y validación de la configuración de cada cliente (/clients/<account_id>.yaml)."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
_RANGO_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class Negocio(BaseModel):
    nombre: str
    descripcion: str = ""
    tono: str = "amable, claro y profesional"
    idioma: str = "español"


class FAQ(BaseModel):
    pregunta: str
    respuesta: str


class Producto(BaseModel):
    # Se permiten campos extra (variantes, medidas, etc.) que se pasan tal cual al modelo.
    model_config = ConfigDict(extra="allow")

    nombre: str
    precio: float | str | None = None
    moneda: str | None = None
    descripcion: str | None = None


class BaseConocimiento(BaseModel):
    model_config = ConfigDict(extra="allow")

    faqs: list[FAQ] = Field(default_factory=list)
    productos: list[Producto] = Field(default_factory=list)
    horarios: str | dict[str, Any] | None = None
    politicas: list[str] | str | None = None


class AtencionHumana(BaseModel):
    zona_horaria: str = "America/Argentina/Buenos_Aires"
    # dia -> lista de rangos "HH:MM-HH:MM". Días ausentes = sin atención.
    horario: dict[str, list[str]] = Field(default_factory=dict)
    mensaje_derivacion: str = (
        "Te comunico con una persona de nuestro equipo, en breve te responde. ¡Gracias por tu paciencia!"
    )
    mensaje_derivacion_fuera_de_horario: str = (
        "Te comunico con una persona de nuestro equipo. En este momento estamos fuera del horario "
        "de atención, así que te van a responder apenas volvamos. ¡Gracias por tu paciencia!"
    )

    @field_validator("zona_horaria")
    @classmethod
    def _zona_valida(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"Zona horaria desconocida: {v!r}. Usá un nombre IANA como America/Argentina/Buenos_Aires "
                "(en Windows hace falta el paquete tzdata: pip install -r requirements.txt)"
            ) from None
        return v

    @field_validator("horario")
    @classmethod
    def _horario_valido(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        normalizado: dict[str, list[str]] = {}
        for dia, rangos in v.items():
            clave = _normalizar_dia(dia)
            if clave not in DIAS:
                raise ValueError(f"Día inválido en horario: {dia!r}")
            for rango in rangos:
                try:
                    _parse_rango(rango)
                except ValueError:
                    raise ValueError(f"Rango horario inválido: {rango!r} (usar HH:MM-HH:MM)") from None
            normalizado[clave] = rangos
        return normalizado

    def disponible(self, ahora: datetime | None = None) -> bool:
        """Indica si hay atención humana en el momento dado (por defecto, ahora)."""
        tz = ZoneInfo(self.zona_horaria)
        ahora = ahora.astimezone(tz) if ahora else datetime.now(tz)
        actual = ahora.time()
        for rango in self.horario.get(DIAS[ahora.weekday()], []):
            inicio, fin = _parse_rango(rango)
            if fin <= inicio:  # rango que cruza la medianoche, ej. 22:00-02:00
                if actual >= inicio or actual < fin:
                    return True
            elif inicio <= actual < fin:
                return True
        return False

    def descripcion_horario(self) -> str:
        if not self.horario:
            return "Sin horario de atención humana configurado."
        lineas = [f"{dia}: {', '.join(self.horario[dia])}" for dia in DIAS if dia in self.horario]
        return "\n".join(lineas) + f"\n(zona horaria: {self.zona_horaria})"


class ChatwootOverrides(BaseModel):
    """Permite que un cliente use tokens propios, leídos de otras variables de entorno."""

    bot_token_env: str | None = None
    api_token_env: str | None = None


class Limites(BaseModel):
    """Topes de respuestas automáticas. Si no se indican, se usan los de las variables de
    entorno. 0 = sin límite."""

    respuestas_por_conversacion_por_hora: int | None = None
    respuestas_por_dia: int | None = None


class ClientConfig(BaseModel):
    account_id: int
    negocio: Negocio
    prompt_sistema: str = ""
    base_conocimiento: BaseConocimiento = Field(default_factory=BaseConocimiento)
    atencion_humana: AtencionHumana = Field(default_factory=AtencionHumana)
    etiqueta_venta: str = "venta"
    limites: Limites = Field(default_factory=Limites)
    chatwoot: ChatwootOverrides = Field(default_factory=ChatwootOverrides)

    def bot_token(self, default: str) -> str:
        return _token_desde_env(self.chatwoot.bot_token_env) or default

    def api_token(self, default: str) -> str:
        return _token_desde_env(self.chatwoot.api_token_env) or default


class ClientNotFound(Exception):
    pass


class ClientRegistry:
    """Lee las configuraciones de disco y las cachea, recargando si el archivo cambia."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: dict[int, tuple[float, ClientConfig]] = {}

    def get(self, account_id: int | str) -> ClientConfig:
        try:
            account = int(account_id)  # también evita path traversal
        except (TypeError, ValueError) as exc:
            raise ClientNotFound(f"account_id inválido: {account_id!r}") from exc

        path = self._path_for(account)
        if path is None:
            raise ClientNotFound(f"No existe configuración para la cuenta {account}")

        mtime = path.stat().st_mtime
        cached = self._cache.get(account)
        if cached and cached[0] == mtime:
            return cached[1]

        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data.setdefault("account_id", account)
        config = ClientConfig.model_validate(data)
        self._cache[account] = (mtime, config)
        logger.info("Configuración cargada para la cuenta %s (%s)", account, config.negocio.nombre)
        return config

    def _path_for(self, account: int) -> Path | None:
        for ext in (".yaml", ".yml"):
            path = self.directory / f"{account}{ext}"
            if path.is_file():
                return path
        return None


def _normalizar_dia(dia: str) -> str:
    return (
        dia.strip().lower()
        .replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    )


def _parse_rango(rango: str) -> tuple[time, time]:
    m = _RANGO_RE.match(rango)
    if m is None:
        raise ValueError(rango)
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    fin = time(23, 59, 59) if (h2, m2) == (24, 0) else time(h2, m2)
    return time(h1, m1), fin


def _token_desde_env(var: str | None) -> str:
    return os.environ.get(var, "") if var else ""
