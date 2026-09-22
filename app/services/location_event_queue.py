"""Cola FIFO acotada que serializa escrituras de ubicación y propaga backpressure."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from app.core.structures.queue import Queue

DEFAULT_LOCATION_QUEUE_LIMIT = 100


class LocationQueueFullError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"La cola de ubicación alcanzó su capacidad máxima de {limit} eventos.")
        self.limit = limit


@dataclass(slots=True)
class _Job[T]:
    run: Callable[[], Awaitable[T]]
    result: asyncio.Future[T]


class LocationEventQueue:
    def __init__(self, max_pending: int = DEFAULT_LOCATION_QUEUE_LIMIT) -> None:
        if max_pending < 1:
            raise ValueError("max_pending debe ser un entero positivo")
        self._jobs: Queue[_Job[object]] = Queue()
        self._max_pending = max_pending
        self._pending = 0
        self._processing = False
        self._guard = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._drain_task: asyncio.Task[None] | None = None

    @property
    def pending_count(self) -> int:
        return self._pending

    async def enqueue[T](self, run: Callable[[], Awaitable[T]], key: str | None = None) -> T:
        # `key` solo lo usa la cola por carriles; una cola simple serializa todo.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()

        async def run_as_object() -> object:
            return await run()

        async with self._guard:
            if self._pending >= self._max_pending:
                raise LocationQueueFullError(self._max_pending)
            self._pending += 1
            self._idle.clear()
            self._jobs.enqueue(_Job(run=run_as_object, result=future))
            if not self._processing:
                self._processing = True
