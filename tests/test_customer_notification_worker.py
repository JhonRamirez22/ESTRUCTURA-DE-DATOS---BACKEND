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
