"""Límite deslizante en memoria para moderar el consumo del proveedor de chat."""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomerChatRateLimitWindow

_CLEANUP_EVERY_CHECKS = 128
_MAX_TRACKED_GUIDES = 4_096
_DATABASE_CLEANUP_EVERY_CHECKS = 128


class CustomerChatRateLimiter:
    """Limita por guía y proceso, sin retener el token de seguimiento en memoria.

    Cada instancia mantiene su propia ventana; varias réplicas no comparten este límite.
    """

    def __init__(
        self,
        *,
        max_requests: int = 12,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("El límite y la ventana deben ser positivos.")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._clock = clock
        self._requests: dict[bytes, deque[float]] = {}
        self._checks_since_cleanup = 0
        self._database_checks = 0

    def retry_after(self, guide: str) -> int | None:
        """Registra una petición o devuelve segundos hasta la siguiente permitida."""
        now = self._clock()
        cutoff = now - self._window_seconds
        guide_digest = hashlib.sha256(guide.encode("utf-8")).digest()
        requests = self._requests.setdefault(guide_digest, deque())
