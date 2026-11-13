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
    queue = LocationEventQueue()
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []

    async def first_job() -> str:
        started.set()
        await release.wait()
        completed.append("first")
        return "first"

    async def following_job() -> str:
        completed.append("following")
        return "following"

    cancelled_request = asyncio.create_task(queue.enqueue(first_job))
    await started.wait()
    cancelled_request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_request

    following = asyncio.create_task(queue.enqueue(following_job))
    draining = asyncio.create_task(queue.wait_until_idle())
    await asyncio.sleep(0)
    assert not draining.done()

    release.set()
    assert await following == "following"
    await draining
    assert completed == ["first", "following"]
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_wait_until_idle_waits_for_all_accepted_jobs() -> None:
    queue = LocationEventQueue()
    started = asyncio.Event()
    release = asyncio.Event()

    async def job() -> int:
        started.set()
        await release.wait()
        return 1

    queued = asyncio.create_task(queue.enqueue(job))
    await started.wait()
    draining = asyncio.create_task(queue.wait_until_idle())
    await asyncio.sleep(0)
    assert not draining.done()

    release.set()
    assert await queued == 1
    await draining
    assert queue.pending_count == 0


def _keys_in_different_lanes(queue: object, lanes: int) -> tuple[str, str]:
    from app.services.location_event_queue import ShardedLocationQueue

    assert isinstance(queue, ShardedLocationQueue)
    first = "courier-0"
    for index in range(1, 500):
        other = f"courier-{index}"
        if queue.lane_of(other) != queue.lane_of(first):
            return first, other
    raise AssertionError("no hay dos claves en carriles distintos")


@pytest.mark.asyncio
async def test_sharded_queue_does_not_let_one_courier_block_another() -> None:
    """H: una sola cola global serializaba a todos los repartidores (503 desde ~250 GPS/s)."""
    from app.services.location_event_queue import ShardedLocationQueue

    queue = ShardedLocationQueue(lanes=4, max_pending_per_lane=10)
    slow_key, fast_key = _keys_in_different_lanes(queue, 4)
    release_slow = asyncio.Event()
    started_slow = asyncio.Event()

    async def slow() -> str:
        started_slow.set()
        await release_slow.wait()
        return "lento"

    async def fast() -> str:
        return "rápido"

    slow_task = asyncio.create_task(queue.enqueue(slow, key=slow_key))
    await started_slow.wait()
    # Con una cola global esto esperaría a que termine el trabajo lento.
    assert await asyncio.wait_for(queue.enqueue(fast, key=fast_key), timeout=1) == "rápido"
    release_slow.set()
    assert await slow_task == "lento"


@pytest.mark.asyncio
async def test_sharded_queue_keeps_fifo_per_courier_and_backpressure_per_lane() -> None:
    from app.services.location_event_queue import ShardedLocationQueue

    queue = ShardedLocationQueue(lanes=4, max_pending_per_lane=2)
    release = asyncio.Event()
    started = asyncio.Event()
    order: list[int] = []

    async def first() -> int:
        started.set()
        await release.wait()
        order.append(1)
        return 1

    async def second() -> int:
        order.append(2)
        return 2

    one = asyncio.create_task(queue.enqueue(first, key="courier-a"))
    await started.wait()
    two = asyncio.create_task(queue.enqueue(second, key="courier-a"))
    await asyncio.sleep(0)
    assert queue.pending_count == 2
    with pytest.raises(LocationQueueFullError):
        await queue.enqueue(second, key="courier-a")
    release.set()
    assert await asyncio.gather(one, two) == [1, 2]
    assert order == [1, 2]
    assert queue.pending_count == 0
    await queue.wait_until_idle()
