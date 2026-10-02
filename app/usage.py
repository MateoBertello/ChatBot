"""Registro de consumo por cliente (tokens de Claude, transcripciones y respuestas).

Cada evento se agrega como una línea JSON en `USAGE_FILE`, para poder auditar y facturar
por cuenta de Chatwoot. El costo es una **estimación** con precios de lista.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# USD por millón de tokens: (entrada, salida, lectura de caché). La escritura en caché
# (TTL 5 min) cuesta 1,25 x la entrada. Actualizar si cambian los precios.
PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    price_in, price_out, price_cache_read = prices
    total = (
        input_tokens * price_in
        + output_tokens * price_out
        + cache_write * price_in * CACHE_WRITE_MULTIPLIER
        + cache_read * price_cache_read
    )
    return round(total / 1_000_000, 6)


class UsageTracker:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = asyncio.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    async def record(self, kind: str, account_id: int, conversation_id: int | None = None, **fields: Any) -> None:
        if self.path is None:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": kind,
            "account_id": account_id,
            "conversation_id": conversation_id,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            async with self._lock:
                await asyncio.to_thread(self._append, line)
        except OSError:
            logger.exception("No se pudo registrar el consumo en %s", self.path)

    def _append(self, line: str) -> None:
        assert self.path is not None
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    async def record_claude(self, account_id: int, conversation_id: int, response: Any) -> None:
        """Registra el uso de una respuesta de la API de Claude.

        Si hubo fallback de modelo, `usage.iterations` trae un registro por intento (cada uno
        con su modelo); si no, se usa el uso total de la respuesta.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        attempts = [
            it for it in (getattr(usage, "iterations", None) or [])
            if getattr(it, "type", None) in ("message", "fallback_message")
        ] or [usage]
        for attempt in attempts:
            model = getattr(attempt, "model", None) or getattr(response, "model", "")
            tokens = {
                "input_tokens": getattr(attempt, "input_tokens", 0) or 0,
                "output_tokens": getattr(attempt, "output_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(attempt, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(attempt, "cache_read_input_tokens", 0) or 0,
            }
            cost = estimate_cost(
                model,
                tokens["input_tokens"],
                tokens["output_tokens"],
                tokens["cache_creation_input_tokens"],
                tokens["cache_read_input_tokens"],
            )
            await self.record("claude", account_id, conversation_id, model=model, cost_usd=cost, **tokens)

    def summarize(self, month: str | None = None, account_id: int | None = None) -> dict[str, Any]:
        """Totales por cuenta, opcionalmente filtrados por mes (`YYYY-MM`) y cuenta."""
        accounts: dict[int, dict[str, Any]] = defaultdict(_empty_summary)
        if self.path is None or not self.path.exists():
            return {"month": month, "accounts": {}}
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if month and not str(entry.get("ts", "")).startswith(month):
                    continue
                if account_id is not None and entry.get("account_id") != account_id:
                    continue
                _accumulate(accounts[entry.get("account_id")], entry)
        for summary in accounts.values():
            summary["costo_estimado_usd"] = round(summary["costo_estimado_usd"], 4)
        return {"month": month, "accounts": {str(k): v for k, v in sorted(accounts.items(), key=lambda kv: str(kv[0]))}}


def _empty_summary() -> dict[str, Any]:
    return {
        "mensajes_atendidos": 0,
        "derivaciones": 0,
        "ventas_registradas": 0,
        "limitados": 0,
        "llamadas_claude": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "costo_estimado_usd": 0.0,
        "modelos_sin_precio": [],
        "transcripciones": 0,
        "audio_bytes": 0,
    }


def _accumulate(summary: dict[str, Any], entry: dict[str, Any]) -> None:
    kind = entry.get("type")
    if kind == "claude":
        summary["llamadas_claude"] += 1
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            summary[key] += int(entry.get(key) or 0)
        if entry.get("cost_usd") is None:
            if entry.get("model") not in summary["modelos_sin_precio"]:
                summary["modelos_sin_precio"].append(entry.get("model"))
        else:
            summary["costo_estimado_usd"] += float(entry["cost_usd"])
    elif kind == "transcription":
        summary["transcripciones"] += 1
        summary["audio_bytes"] += int(entry.get("bytes") or 0)
    elif kind == "run":
        summary["mensajes_atendidos"] += 1
        summary["derivaciones"] += int(bool(entry.get("handed_off")))
        summary["ventas_registradas"] += int(bool(entry.get("interest_registered")))
        summary["limitados"] += int(entry.get("outcome") == "rate_limited")
