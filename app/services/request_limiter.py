"""Límite de peticiones por cuenta en ventana deslizante (en memoria, por proceso).

Frena repeticiones masivas de una misma sesión (por ejemplo, reenviar una petición cientos
de veces desde las herramientas del navegador) sin afectar el uso normal de la interfaz.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit y window_seconds deben ser positivos.")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str) -> int | None:
        """Registra la petición y devuelve los segundos de espera si se superó el límite."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, math.ceil(self._window - (now - hits[0])))
        hits.append(now)
        if len(self._hits) > MAX_TRACKED_KEYS:
            self._evict(now)
        return None

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= self._window]:
            del self._hits[key]
        if len(self._hits) > MAX_TRACKED_KEYS:
            self._hits.clear()
