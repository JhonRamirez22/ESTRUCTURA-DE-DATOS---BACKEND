from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.couriers import get_location_event_queue, get_write_session_factory
from app.core.auth import (
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    session_cookie_name,
    verify_courier_access_code,
)
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierCredential,
    CourierStatus,
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteStatus,
)
from app.services.location_event_queue import LocationEventQueue
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_DISPATCHER_ID = "f8fbc95f-643e-4fb3-9b82-8c42e0707311"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'couriers.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(connection: Connection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", _SESSION_SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.setenv("NOTIFICATION_SMTP_HOST", "")
    monkeypatch.setenv("NOTIFICATION_SMTP_USERNAME", "")
    monkeypatch.setenv("NOTIFICATION_SMTP_PASSWORD", "")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "")
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_AUTH_TOKEN", "")
