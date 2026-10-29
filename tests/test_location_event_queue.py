from __future__ import annotations

import asyncio

import pytest

from app.services.location_event_queue import LocationEventQueue, LocationQueueFullError


@pytest.mark.asyncio
async def test_location_queue_preserves_fifo_and_applies_capacity_backpressure() -> None:
    queue = LocationEventQueue(max_pending=2)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execution_order: list[int] = []

    async def first_job() -> int:
        first_started.set()
        await release_first.wait()
        execution_order.append(1)
        return 1

    async def second_job() -> int:
        execution_order.append(2)
        return 2

    first = asyncio.create_task(queue.enqueue(first_job))
    await first_started.wait()
    second = asyncio.create_task(queue.enqueue(second_job))
    await asyncio.sleep(0)
    assert queue.pending_count == 2

    with pytest.raises(LocationQueueFullError, match="capacidad máxima de 2"):
        await queue.enqueue(second_job)

    release_first.set()
    assert await asyncio.gather(first, second) == [1, 2]
    assert execution_order == [1, 2]
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_failed_location_write_does_not_block_following_jobs() -> None:
    queue = LocationEventQueue()
    completed: list[str] = []

    async def failing_job() -> None:
        raise RuntimeError("write failed")

    async def following_job() -> str:
        completed.append("saved")
        return "saved"

    failed = asyncio.create_task(queue.enqueue(failing_job))
    following = asyncio.create_task(queue.enqueue(following_job))
    with pytest.raises(RuntimeError, match="write failed"):
        await failed
    assert await following == "saved"
    assert completed == ["saved"]


@pytest.mark.asyncio
async def test_cancelled_request_does_not_abandon_accepted_location_write() -> None:
