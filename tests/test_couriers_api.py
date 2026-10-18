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
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
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
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_FROM_PHONE", "")
    get_settings.cache_clear()
    queue = LocationEventQueue()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_write_session_factory] = lambda: session_factory
    app.dependency_overrides[get_location_event_queue] = lambda: queue
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_write_session_factory, None)
        app.dependency_overrides.pop(get_location_event_queue, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: str | None = None) -> str:
    token = create_session_token(
        role,
        int(datetime.now(UTC).timestamp()) + SESSION_MAX_AGE_SECONDS,
        _SESSION_SECRET,
        courier_id,
    )
    return f"{SESSION_COOKIE_NAME}={token}"


async def _create_courier(
    session_factory: async_sessionmaker[AsyncSession],
    courier_id: UUID | None = None,
) -> UUID:
    identifier = courier_id or uuid4()
    async with session_factory() as session:
        session.add(
            Courier(
                id=identifier,
                name="Mensajero de prueba",
                phone="3000000000",
                status=CourierStatus.OFFLINE,
            )
        )
        await session.commit()
    return identifier


@pytest.mark.asyncio
async def test_courier_collection_and_access_code_require_dispatcher(
    client: httpx.AsyncClient,
) -> None:
    anonymous = await client.get("/api/repartidores")
    courier = await client.get(
        "/api/repartidores", headers={"Cookie": _cookie("courier", _DISPATCHER_ID)}
    )
    renew_code = await client.post("/api/repartidores/invalid/codigo")

    assert anonymous.status_code == 401
    assert courier.status_code == 403
    assert renew_code.status_code == 401


@pytest.mark.asyncio
async def test_dispatcher_can_create_list_update_and_delete_courier_without_exposing_hash(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher = {"Cookie": _cookie("dispatcher")}
    created = await client.post(
        "/api/repartidores",
        headers=dispatcher,
        json={"name": "  Repartidor nuevo  ", "phone": " 3001234567 "},
