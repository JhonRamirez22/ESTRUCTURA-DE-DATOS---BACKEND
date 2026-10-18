from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from sqlite3 import Connection
from time import time
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
)
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    Route,
    RouteStatus,
)
from app.services.address_advisor import (
    AddressAiRequest,
    AddressAiResponse,
    get_address_ai_advisor,
)
from app.services.customer_notifications import enqueue_order_assigned_notification
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = "f8fbc95f-643e-4fb3-9b82-8c42e0707311"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orders.sqlite'}")

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
    monkeypatch.setenv("AI_ALLOW_LOCATION_DATA_SHARING", "false")
    get_settings.cache_clear()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_address_ai_advisor, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: str | None = None) -> str:
    token = create_session_token(
        role,
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SESSION_SECRET,
        courier_id,
    )
    return f"{SESSION_COOKIE_NAME}={token}"


async def _create_courier(session_factory: async_sessionmaker[AsyncSession]) -> UUID:
    courier_id = UUID(_COURIER_ID)
    async with session_factory() as session:
        session.add(
            Courier(
                id=courier_id,
                name="Repartidor de prueba",
                phone="3000000000",
                status=CourierStatus.ON_ROUTE,
            )
        )
        await session.commit()
    return courier_id


async def _create_delivery_point(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    route_id: UUID | None = None,
    status: DeliveryPointStatus = DeliveryPointStatus.PENDING,
) -> UUID:
    point_id = uuid4()
    async with session_factory() as session:
        session.add(
            DeliveryPoint(
                id=point_id,
                address="Carrera 25 # 4 sur 65",
                lat=1.2136,
                lng=-77.2811,
                status=status,
                order_id=f"PED-{point_id}",
                tracking_token=f"guide-{point_id}",
                route_id=route_id,
                sequence_index=0 if route_id else None,
            )
        )
        await session.commit()
    return point_id


@pytest.mark.asyncio
async def test_order_collection_requires_dispatcher_and_creates_normalized_point(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    denied = await client.post(
        "/api/pedidos",
        json={"address": "cra 25 num 4 sur 65", "lat": 1.2136, "lng": -77.2811, "orderId": "P-1"},
    )
    assert denied.status_code == 401

    created = await client.post(
        "/api/pedidos",
        headers={"Cookie": _cookie("dispatcher")},
        json={
            "address": "  cra 25 num 4 sur 65 ",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "P-1",
        },
    )
    payload = created.json()
    assert created.status_code == 201
    assert payload["deliveryPoint"]["address"] == "Carrera 25 # 4 sur 65"
    assert payload["deliveryPoint"]["status"] == "PENDING"
    assert payload["trackingGuide"] == payload["deliveryPoint"]["trackingToken"]
    assert payload["addressNormalization"]["source"] == "deterministic"
    assert created.headers["cache-control"] == "no-store, private, max-age=0"

    async with session_factory() as session:
        persisted = await session.scalar(
            select(DeliveryPoint).where(DeliveryPoint.order_id == "P-1")
        )
        assert persisted is not None
        assert persisted.lat == 1.2136
        assert persisted.lng == -77.2811

    listed = await client.get(
        "/api/pedidos?status=PENDING", headers={"Cookie": _cookie("dispatcher")}
    )
    assert listed.status_code == 200
    assert [item["orderId"] for item in listed.json()["deliveryPoints"]] == ["P-1"]


@pytest.mark.asyncio
async def test_order_input_rejects_bad_json_outside_pasto_and_foreign_city(
    client: httpx.AsyncClient,
) -> None:
    headers = {"Cookie": _cookie("dispatcher")}
    malformed = await client.post(
        "/api/pedidos", headers={**headers, "Content-Type": "application/json"}, content="{"
    )
    outside = await client.post(
        "/api/pedidos",
        headers=headers,
        json={"address": "Calle 1", "lat": 40.7128, "lng": -74.006, "orderId": "P-2"},
