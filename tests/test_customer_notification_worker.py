from __future__ import annotations

import asyncio
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.main as application
import app.services.customer_notification_worker as worker
from app.services.customer_notifications import DispatchSummary
from app.settings import Settings


def _unused_session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], object())


@pytest.mark.asyncio
async def test_worker_processes_immediately_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    stop_event = asyncio.Event()

    async def dispatch(_session_factory: async_sessionmaker[AsyncSession]) -> DispatchSummary:
        nonlocal calls
        calls += 1
        stop_event.set()
        return DispatchSummary(configured=True, attempted=1, sent=1)

    monkeypatch.setattr(worker, "dispatch_pending_customer_notifications", dispatch)

    await worker.run_customer_notification_worker(
        _unused_session_factory(), stop_event, poll_interval_seconds=1
    )

    assert calls == 1


@pytest.mark.asyncio
async def test_worker_survives_a_transient_outbox_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    stop_event = asyncio.Event()

    async def dispatch(_session_factory: async_sessionmaker[AsyncSession]) -> DispatchSummary:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database detail must not be logged")
        stop_event.set()
        return DispatchSummary(configured=True)

    monkeypatch.setattr(worker, "dispatch_pending_customer_notifications", dispatch)

    await worker.run_customer_notification_worker(
        _unused_session_factory(), stop_event, poll_interval_seconds=0.001
    )

    assert calls == 2


def test_worker_rejects_a_non_positive_poll_interval() -> None:
    with pytest.raises(ValueError, match="debe ser positivo"):
        asyncio.run(
            worker.run_customer_notification_worker(
                _unused_session_factory(), asyncio.Event(), poll_interval_seconds=0
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_enabled", "is_vercel"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_fastapi_lifespan_starts_worker_only_when_enabled_on_a_persistent_runtime(
    monkeypatch: pytest.MonkeyPatch,
    worker_enabled: bool,
    is_vercel: bool,
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def wait_for_shutdown(
        _session_factory: async_sessionmaker[AsyncSession],
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float,
    ) -> None:
        assert poll_interval_seconds == Settings().customer_notification_poll_seconds
        started.set()
        await stop_event.wait()
        stopped.set()

    async def no_op() -> None:
        return None

    class _IdleLocationQueue:
        async def wait_until_idle(self) -> None:
            return None

    monkeypatch.setattr(
        application,
        "get_settings",
        lambda: Settings(customer_notification_worker_enabled=worker_enabled),
    )
    if is_vercel:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(
        application,
        "any_customer_notification_delivery_configured",
        lambda _settings: True,
    )
    monkeypatch.setattr(application, "get_session_factory", _unused_session_factory)
    monkeypatch.setattr(application, "run_customer_notification_worker", wait_for_shutdown)
    monkeypatch.setattr(application, "get_location_event_queue", _IdleLocationQueue)
    monkeypatch.setattr(application, "close_routing_clients", no_op)
    monkeypatch.setattr(application, "close_traffic_service", no_op)
    monkeypatch.setattr(application, "dispose_engine", no_op)

    worker_should_run = worker_enabled and not is_vercel
    async with application.lifespan(application.app):
        if worker_should_run:
            await asyncio.wait_for(started.wait(), timeout=1)

    assert started.is_set() is worker_should_run
    assert stopped.is_set() is worker_should_run
