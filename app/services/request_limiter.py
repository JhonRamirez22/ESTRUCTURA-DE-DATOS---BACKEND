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
