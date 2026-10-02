"""Límites de uso en memoria (ventana deslizante) para acotar spam y costo."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}

    def allow(self, key: Hashable, limit: int, window_seconds: float) -> bool:
        """Registra un uso y devuelve False si `key` ya alcanzó `limit` en la ventana.

        Un límite de 0 o negativo significa "sin límite".
        """
        if limit <= 0:
            return True
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def prune(self, max_window_seconds: float) -> None:
        """Libera claves sin usos recientes (para que la memoria no crezca sin límite)."""
        now = self._clock()
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] >= max_window_seconds]:
            del self._hits[key]
