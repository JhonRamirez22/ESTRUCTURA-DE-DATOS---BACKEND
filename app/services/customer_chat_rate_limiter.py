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
        while requests and requests[0] <= cutoff:
            requests.popleft()

        if len(requests) >= self._max_requests:
            return max(1, math.ceil(requests[0] + self._window_seconds - now))

        requests.append(now)
        self._checks_since_cleanup += 1
        if (
            self._checks_since_cleanup >= _CLEANUP_EVERY_CHECKS
            or len(self._requests) > _MAX_TRACKED_GUIDES
        ):
            self._remove_expired(cutoff)
            self._checks_since_cleanup = 0
        return None

    async def retry_after_shared(
        self,
        session: AsyncSession,
        guide: str,
        *,
        hmac_secret: str,
        now: datetime | None = None,
    ) -> int | None:
        """Registra el turno en PostgreSQL/SQLite con UPSERT atómico compartido.

        HMAC evita persistir la guía y evita que una filtración de la tabla permita
        comprobar guías candidatas sin conocer el secreto de autenticación.
        """
        if not hmac_secret:
            raise ValueError("AUTH_SECRET debe configurarse para limitar el chat.")

        instant = _as_naive_utc(now or datetime.now(UTC))
        cutoff = instant - timedelta(seconds=self._window_seconds)
        guide_digest = hmac.new(
            hmac_secret.encode("utf-8"),
            guide.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        dialect_name = session.get_bind().dialect.name
        insert_statement: Any
        if dialect_name == "postgresql":
            insert_statement = postgres_insert(CustomerChatRateLimitWindow)
        elif dialect_name == "sqlite":
            insert_statement = sqlite_insert(CustomerChatRateLimitWindow)
        else:
            raise RuntimeError("El límite compartido requiere PostgreSQL o SQLite.")

        expired = CustomerChatRateLimitWindow.window_started_at <= cutoff
        statement = (
            insert_statement.values(
                guide_hash=guide_digest,
                window_started_at=instant,
                request_count=1,
            )
            .on_conflict_do_update(
                index_elements=[CustomerChatRateLimitWindow.guide_hash],
                set_={
                    "window_started_at": case(
                        (expired, instant),
                        else_=CustomerChatRateLimitWindow.window_started_at,
                    ),
                    "request_count": case(
                        (expired, 1),
                        (
                            CustomerChatRateLimitWindow.request_count < self._max_requests + 1,
                            CustomerChatRateLimitWindow.request_count + 1,
                        ),
                        else_=self._max_requests + 1,
                    ),
                },
            )
            .returning(
                CustomerChatRateLimitWindow.window_started_at,
                CustomerChatRateLimitWindow.request_count,
            )
        )
        stored_window_start, stored_request_count = (await session.execute(statement)).one()
        window_started_at = cast(datetime, stored_window_start)
        request_count = cast(int, stored_request_count)

        self._database_checks += 1
        if self._database_checks % _DATABASE_CLEANUP_EVERY_CHECKS == 0:
            await session.execute(
                delete(CustomerChatRateLimitWindow).where(
                    CustomerChatRateLimitWindow.window_started_at <= cutoff
                )
            )
        await session.commit()

        if request_count <= self._max_requests:
            return None
        retry_at = window_started_at + timedelta(seconds=self._window_seconds)
        return max(1, math.ceil((retry_at - instant).total_seconds()))

    def _remove_expired(self, cutoff: float) -> None:
        for guide_digest, requests in tuple(self._requests.items()):
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if not requests:
                del self._requests[guide_digest]


def _as_naive_utc(value: datetime) -> datetime:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.replace(tzinfo=None)
