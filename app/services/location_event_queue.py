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

    @property
    def pending_count(self) -> int:
        return self._pending

    async def enqueue[T](self, run: Callable[[], Awaitable[T]]) -> T:
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
                loop.create_task(self._drain())
        return cast(T, await future)

    async def wait_until_idle(self) -> None:
        """Espera a que terminen las escrituras aceptadas antes de cerrar PostgreSQL."""
        await self._idle.wait()

    async def _drain(self) -> None:
        while True:
            async with self._guard:
                job = self._jobs.dequeue()
                if job is None:
                    self._processing = False
                    self._idle.set()
                    return
            try:
                result = await job.run()
            except Exception as error:
                if not job.result.done():
                    job.result.set_exception(error)
            else:
                if not job.result.done():
                    job.result.set_result(result)
            finally:
                async with self._guard:
                    self._pending -= 1
