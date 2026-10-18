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
    )

    assert created.status_code == 201
    payload = created.json()
    courier = payload["courier"]
    courier_id = UUID(courier["id"])
    access_code = payload["accessCode"]
    assert courier["name"] == "Repartidor nuevo"
    assert courier["status"] == CourierStatus.OFFLINE.value
    assert "credential" not in courier
    assert created.headers["cache-control"] == "no-store, private"

    async with session_factory() as session:
        credential = await session.get(CourierCredential, courier_id)
        assert credential is not None
        assert verify_courier_access_code(access_code, credential.access_code_hash)
        assert access_code not in credential.access_code_hash

    updated = await client.patch(
        f"/api/repartidores/{courier_id}", headers=dispatcher, json={"name": "Actualizado"}
    )
    listed = await client.get("/api/repartidores?status=OFFLINE", headers=dispatcher)
    assert updated.status_code == 200
    assert updated.json()["courier"]["name"] == "Actualizado"
    assert any(item["id"] == str(courier_id) for item in listed.json()["couriers"])

    deleted = await client.delete(f"/api/repartidores/{courier_id}", headers=dispatcher)
    assert deleted.status_code == 204
    async with session_factory() as session:
        assert await session.get(Courier, courier_id) is None
        assert await session.get(CourierCredential, courier_id) is None


@pytest.mark.asyncio
async def test_courier_create_rejects_spoofed_gps_and_delete_preserves_route_history(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher = {"Cookie": _cookie("dispatcher")}
    spoofed = await client.post(
        "/api/repartidores",
        headers=dispatcher,
        json={"name": "No GPS", "phone": "3001111111", "currentLat": 1.21},
    )
    assert spoofed.status_code == 400
    assert spoofed.json()["error"].startswith("La posición no se define")

    courier_id = await _create_courier(session_factory)
    async with session_factory() as session:
        session.add(
            Route(
                courier_id=courier_id,
                status=RouteStatus.PLANNED,
                estimated_duration_minutes=5,
                estimated_distance_meters=500,
            )
        )
        await session.commit()
    response = await client.delete(f"/api/repartidores/{courier_id}", headers=dispatcher)

    assert response.status_code == 409
    assert response.json() == {"error": "No se puede eliminar un repartidor con rutas asociadas."}


@pytest.mark.asyncio
async def test_authenticated_location_is_validated_persisted_and_updates_courier(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    recorded_at = datetime.now(UTC) - timedelta(seconds=2)
    response = await client.post(
        "/api/repartidores/me/ubicacion",
        headers={"Cookie": _cookie("courier", str(courier_id))},
        json={
            "lat": 1.2136,
            "lng": -77.2811,
            "recordedAt": recorded_at.isoformat().replace("+00:00", "Z"),
        },
    )

    assert response.status_code == 201
    assert response.json()["accepted"] is True
    assert response.json()["locationEvent"]["courierId"] == str(courier_id)
    assert response.json()["locationEvent"]["recordedAt"].endswith("Z")
    async with session_factory() as session:
        courier = await session.get(Courier, courier_id)
        events = await session.scalars(
            select(LocationEvent).where(LocationEvent.courier_id == courier_id)
        )
        assert courier is not None
        assert courier.status is CourierStatus.AVAILABLE
        assert courier.current_lat == 1.2136
        assert len(events.all()) == 1

    me = await client.get(
        "/api/repartidores/me", headers={"Cookie": _cookie("courier", str(courier_id))}
    )
    assert me.status_code == 200
    assert me.json()["courier"]["currentLat"] == 1.2136
    assert "phone" not in me.json()["courier"]


@pytest.mark.asyncio
async def test_location_rejects_other_courier_and_coordinates_outside_pasto(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    other_id = uuid4()
    other_courier = await client.post(
        f"/api/repartidores/{courier_id}/ubicacion",
        headers={"Cookie": _cookie("courier", str(other_id))},
        json={"lat": 1.2136, "lng": -77.2811},
    )
    outside = await client.post(
        "/api/repartidores/me/ubicacion",
        headers={"Cookie": _cookie("courier", str(courier_id))},
        json={"lat": 40.7128, "lng": -74.006},
    )

    assert other_courier.status_code == 403
    assert outside.status_code == 400
    async with session_factory() as session:
        assert await session.scalar(select(LocationEvent.id)) is None


@pytest.mark.asyncio
async def test_old_location_is_recorded_but_cannot_replace_newer_fix_and_stop_goes_offline(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    latest = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)
    async with session_factory() as session:
        courier = await session.get(Courier, courier_id)
        assert courier is not None
        courier.current_lat = 1.22
        courier.current_lng = -77.27
        courier.last_location_at = latest
        await session.commit()

    older = await client.post(
        "/api/repartidores/me/ubicacion",
        headers={"Cookie": _cookie("courier", str(courier_id))},
        json={
            "lat": 1.2136,
            "lng": -77.2811,
            "recordedAt": (latest - timedelta(seconds=2)).isoformat() + "Z",
        },
    )
    stopped = await client.delete(
        "/api/repartidores/me/ubicacion",
        headers={"Cookie": _cookie("courier", str(courier_id))},
    )

    assert older.status_code == 201
    assert older.json()["accepted"] is False
    assert stopped.status_code == 200
    assert stopped.json()["courier"]["status"] == CourierStatus.OFFLINE.value
    async with session_factory() as session:
        courier = await session.get(Courier, courier_id)
        assert courier is not None
        assert courier.current_lat == 1.22
        assert courier.current_lng == -77.27
        assert courier.last_location_at == latest


@pytest.mark.asyncio
async def test_accepted_gps_queues_one_nearby_notice_per_opted_in_channel(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    route_id = uuid4()
    point_id = uuid4()
    async with session_factory() as session:
        session.add(
            Route(
                id=route_id,
                courier_id=courier_id,
                status=RouteStatus.IN_PROGRESS,
                estimated_duration_minutes=8,
                estimated_distance_meters=900,
            )
        )
        session.add(
            DeliveryPoint(
                id=point_id,
                address="Carrera 25 # 4 sur 65, Pasto",
                lat=1.214,
                lng=-77.2811,
                status=DeliveryPointStatus.EN_ROUTE,
                order_id="PEDIDO-CERCA",
                customer_email="cliente@example.com",
                email_notifications_enabled=True,
                customer_phone="+573001234567",
                sms_notifications_enabled=True,
                tracking_token="guide-nearby-test",
                route_id=route_id,
                sequence_index=0,
            )
        )
        await session.commit()

    headers = {"Cookie": _cookie("courier", str(courier_id))}
    response = await client.post(
        "/api/repartidores/me/ubicacion",
        headers=headers,
        json={
            "lat": 1.2136,
            "lng": -77.2811,
            "recordedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
    repeated_position = await client.post(
        "/api/repartidores/me/ubicacion",
        headers=headers,
        json={
            "lat": 1.2136,
            "lng": -77.2811,
            "recordedAt": (datetime.now(UTC) + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )

    assert response.status_code == 201
    assert response.json()["accepted"] is True
    assert repeated_position.status_code == 201
    assert repeated_position.json()["accepted"] is True
    assert "cliente@example.com" not in response.text
    assert "+573001234567" not in response.text
    async with session_factory() as session:
        notices = list((await session.scalars(select(CustomerNotificationOutbox))).all())
        assert len(notices) == 2
        assert {
            (notice.channel, notice.event_type, notice.delivery_point_id, notice.status)
            for notice in notices
        } == {
            (
                CustomerNotificationChannel.EMAIL,
                CustomerNotificationEvent.COURIER_NEAR,
                point_id,
                CustomerNotificationStatus.PENDING,
            ),
            (
                CustomerNotificationChannel.SMS,
                CustomerNotificationEvent.COURIER_NEAR,
                point_id,
                CustomerNotificationStatus.PENDING,
            ),
        }


@pytest.mark.asyncio
async def test_gps_does_not_notify_nearby_customer_before_route_starts(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    async with session_factory() as session:
        route = Route(
            id=uuid4(),
            courier_id=courier_id,
            status=RouteStatus.PLANNED,
            estimated_duration_minutes=8,
            estimated_distance_meters=900,
        )
        point = DeliveryPoint(
            id=uuid4(),
            address="Carrera 25 # 4 sur 65, Pasto",
            lat=1.2136,
            lng=-77.2811,
            status=DeliveryPointStatus.PENDING,
            order_id="INTERNAL-REFERENCE-MUST-NOT-BE-SENT",
            customer_email="cliente@example.com",
            email_notifications_enabled=True,
            customer_phone="+573001234567",
            sms_notifications_enabled=True,
            tracking_token="guide-planned-route-test",
            route_id=route.id,
            sequence_index=0,
        )
        session.add_all([route, point])
        await session.commit()

    response = await client.post(
        "/api/repartidores/me/ubicacion",
        headers={"Cookie": _cookie("courier", str(courier_id))},
        json={
            "lat": 1.2136,
            "lng": -77.2811,
            "recordedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )

    assert response.status_code == 201
    assert response.json()["accepted"] is True
    async with session_factory() as session:
        notices = list((await session.scalars(select(CustomerNotificationOutbox))).all())
    assert notices == []
