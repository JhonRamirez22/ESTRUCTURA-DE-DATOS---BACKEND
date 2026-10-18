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
    )
    foreign = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            "address": "Carrera 25, Bogotá",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "P-3",
        },
    )
    assert malformed.status_code == 400
    assert malformed.json() == {"error": "El body debe ser JSON válido."}
    assert outside.status_code == 400
    assert "dentro de la zona de servicio de Pasto" in outside.json()["error"]
    assert foreign.status_code == 400
    assert "ciudad fuera de Pasto" in foreign.json()["error"]


@pytest.mark.asyncio
async def test_customer_email_requires_consent_and_is_never_returned_in_order_json(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = {"Cookie": _cookie("dispatcher")}
    missing_contact = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            "address": "Calle 10",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "CONSENT-MISSING",
            "emailNotificationsEnabled": True,
        },
    )
    invalid_email = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            "address": "Calle 10",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "EMAIL-INVALID",
            "customerEmail": "no-es-correo",
            "emailNotificationsEnabled": True,
        },
    )
    unconsented_email = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            "address": "Calle 10",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "EMAIL-NO-CONSENT",
            "customerEmail": "no-retener@example.com",
            "emailNotificationsEnabled": False,
        },
    )
    created = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            "address": "Calle 10",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "EMAIL-CONSENT",
            "customerEmail": "cliente@example.com",
            "emailNotificationsEnabled": True,
        },
    )

    assert missing_contact.status_code == 400
    assert invalid_email.status_code == 400
    assert unconsented_email.status_code == 201
    assert created.status_code == 201
    assert "no-retener@example.com" not in unconsented_email.text
    assert "cliente@example.com" not in created.text
    point_id = UUID(created.json()["deliveryPoint"]["id"])
    unconsented_id = UUID(unconsented_email.json()["deliveryPoint"]["id"])
    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        assert saved.customer_email == "cliente@example.com"
        assert saved.email_notifications_enabled is True
        unconsented = await session.get(DeliveryPoint, unconsented_id)
        assert unconsented is not None
        assert unconsented.customer_email is None
        assert unconsented.email_notifications_enabled is False

    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        await enqueue_order_assigned_notification(session, saved, uuid4())
        await session.commit()

    revoked = await client.patch(
        f"/api/pedidos/{point_id}",
        headers=headers,
        json={"emailNotificationsEnabled": False},
    )
    assert revoked.status_code == 200, revoked.text
    assert "cliente@example.com" not in revoked.text
    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        assert saved.customer_email is None
        assert saved.email_notifications_enabled is False
        notification = await session.scalar(
            select(CustomerNotificationOutbox).where(
                CustomerNotificationOutbox.delivery_point_id == point_id
            )
        )
        assert notification is not None
        assert notification.channel is CustomerNotificationChannel.EMAIL
        assert notification.event_type is CustomerNotificationEvent.ORDER_ASSIGNED
        assert notification.status is CustomerNotificationStatus.CANCELLED
        assert notification.recipient_email is None
        assert notification.subject == ""
        assert notification.text_body == ""


@pytest.mark.asyncio
async def test_customer_sms_requires_explicit_consent_and_normalizes_colombian_mobile(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = {"Cookie": _cookie("dispatcher")}
    base_order = {
        "address": "Calle 10",
        "lat": 1.2136,
        "lng": -77.2811,
    }
    missing_phone = await client.post(
        "/api/pedidos",
        headers=headers,
        json={**base_order, "orderId": "SMS-MISSING", "smsNotificationsEnabled": True},
    )
    invalid_phone = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            **base_order,
            "orderId": "SMS-INVALID",
            "customerPhone": "+12025550100",
            "smsNotificationsEnabled": True,
        },
    )
    unconsented_phone = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            **base_order,
            "orderId": "SMS-NO-CONSENT",
            "customerPhone": "3001234567",
        },
    )
    opted_in = await client.post(
        "/api/pedidos",
        headers=headers,
        json={
            **base_order,
            "orderId": "SMS-CONSENT",
            "customerPhone": "300 123 4567",
            "smsNotificationsEnabled": True,
        },
    )

    assert missing_phone.status_code == 400
    assert invalid_phone.status_code == 400
    assert unconsented_phone.status_code == 201
    assert opted_in.status_code == 201
    assert "3001234567" not in opted_in.text
    point_id = UUID(opted_in.json()["deliveryPoint"]["id"])
    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        assert saved.customer_phone == "+573001234567"
        assert saved.sms_notifications_enabled is True
        unconsented_id = UUID(unconsented_phone.json()["deliveryPoint"]["id"])
        unconsented = await session.get(DeliveryPoint, unconsented_id)
        assert unconsented is not None
        assert unconsented.customer_phone is None
        assert unconsented.sms_notifications_enabled is False

    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        await enqueue_order_assigned_notification(session, saved, uuid4())
        await session.commit()

    revoked = await client.patch(
        f"/api/pedidos/{point_id}",
        headers=headers,
        json={"smsNotificationsEnabled": False},
    )
    assert revoked.status_code == 200, revoked.text
    async with session_factory() as session:
        saved = await session.get(DeliveryPoint, point_id)
        assert saved is not None
        assert saved.customer_phone is None
        assert saved.sms_notifications_enabled is False
        notification = await session.scalar(
            select(CustomerNotificationOutbox).where(
                CustomerNotificationOutbox.delivery_point_id == point_id
            )
        )
        assert notification is not None
        assert notification.channel is CustomerNotificationChannel.SMS
        assert notification.status is CustomerNotificationStatus.CANCELLED
        assert notification.recipient_phone is None
        assert notification.subject == ""
        assert notification.text_body == ""


@pytest.mark.asyncio
async def test_duplicate_order_id_returns_conflict_and_get_is_private(
    client: httpx.AsyncClient,
) -> None:
    headers = {"Cookie": _cookie("dispatcher")}
    body = {
        "address": "Calle 10",
        "lat": 1.2136,
        "lng": -77.2811,
        "orderId": "DUPLICATE",
    }
    created = await client.post("/api/pedidos", headers=headers, json=body)
    duplicate = await client.post("/api/pedidos", headers=headers, json=body)
    point_id = created.json()["deliveryPoint"]["id"]
    anonymous_read = await client.get(f"/api/pedidos/{point_id}")
    dispatcher_read = await client.get(f"/api/pedidos/{point_id}", headers=headers)

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert anonymous_read.status_code == 401
    assert dispatcher_read.status_code == 200
    assert (
        dispatcher_read.json()["deliveryPoint"]["trackingToken"]
        == created.json()["trackingGuide"]
    )


@pytest.mark.asyncio
async def test_courier_can_only_close_own_active_route_delivery_and_token_is_hidden(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = await _create_courier(session_factory)
    route_id = uuid4()
    async with session_factory() as session:
        session.add(
            Route(
                id=route_id,
                courier_id=courier_id,
                status=RouteStatus.IN_PROGRESS,
                estimated_duration_minutes=10,
                estimated_distance_meters=1_000,
            )
        )
        await session.commit()
    point_id = await _create_delivery_point(session_factory, route_id=route_id)
    own_cookie = {"Cookie": _cookie("courier", str(courier_id))}
    other_cookie = {"Cookie": _cookie("courier", str(uuid4()))}

    forbidden = await client.patch(
        f"/api/pedidos/{point_id}", headers=other_cookie, json={"status": "DELIVERED"}
    )
    changed_fields = await client.patch(
        f"/api/pedidos/{point_id}",
        headers=own_cookie,
        json={"status": "DELIVERED", "orderId": "spoof"},
    )
    completed = await client.patch(
        f"/api/pedidos/{point_id}", headers=own_cookie, json={"status": "DELIVERED"}
    )
    repeated = await client.patch(
        f"/api/pedidos/{point_id}", headers=own_cookie, json={"status": "FAILED"}
    )

    assert forbidden.status_code == 404
    assert changed_fields.status_code == 403
    assert completed.status_code == 200
    assert completed.json()["deliveryPoint"]["status"] == "DELIVERED"
    assert "trackingToken" not in completed.json()["deliveryPoint"]
    assert repeated.status_code == 409
    async with session_factory() as session:
        notifications = list((await session.scalars(select(Notification))).all())
        persisted = await session.get(DeliveryPoint, point_id)
        assert persisted is not None
        assert persisted.status is DeliveryPointStatus.DELIVERED
        assert len(notifications) == 1
        assert notifications[0].recipient_id == "dispatcher"


@pytest.mark.asyncio
async def test_dispatcher_can_edit_and_delete_only_unassigned_pending_orders(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dispatcher = {"Cookie": _cookie("dispatcher")}
    created = await client.post(
        "/api/pedidos",
        headers=dispatcher,
        json={
            "address": "Calle 2",
            "lat": 1.2136,
            "lng": -77.2811,
            "orderId": "EDIT-ME",
            "timeWindow": {"start": "09:00", "end": "12:00"},
        },
    )
    point_id = UUID(created.json()["deliveryPoint"]["id"])
    updated = await client.patch(
        f"/api/pedidos/{point_id}",
        headers=dispatcher,
        json={"address": "cra 3", "timeWindow": None},
    )
    assert updated.status_code == 200
    assert updated.json()["deliveryPoint"]["address"] == "Carrera 3"
    assert updated.json()["deliveryPoint"]["timeWindow"] is None

    assigned_id = uuid4()
    courier_id = await _create_courier(session_factory)
    async with session_factory() as session:
        session.add(
            Route(
                id=assigned_id,
                courier_id=courier_id,
                status=RouteStatus.PLANNED,
                estimated_duration_minutes=10,
                estimated_distance_meters=1_000,
            )
        )
        await session.commit()
    assigned_point_id = await _create_delivery_point(session_factory, route_id=assigned_id)

    blocked_delete = await client.delete(f"/api/pedidos/{assigned_point_id}", headers=dispatcher)
    blocked_location_edit = await client.patch(
        f"/api/pedidos/{assigned_point_id}",
        headers=dispatcher,
        json={"lat": 1.215, "lng": -77.279},
    )
    deleted = await client.delete(f"/api/pedidos/{point_id}", headers=dispatcher)
    assert blocked_delete.status_code == 409
    assert blocked_location_edit.status_code == 409
    assert deleted.status_code == 204
    async with session_factory() as session:
        assert await session.get(DeliveryPoint, point_id) is None
        assigned_point = await session.get(DeliveryPoint, assigned_point_id)
        assert assigned_point is not None
        assert assigned_point.lat == 1.2136
        assert assigned_point.lng == -77.2811


@pytest.mark.asyncio
async def test_address_ai_requires_opt_in_and_rejects_location_outside_pasto(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdvisor:
        async def normalize_address(self, _request: AddressAiRequest) -> AddressAiResponse:
            return AddressAiResponse(
                normalizedAddress="Calle 100, Quito",
                isInPasto=True,
                confidence=0.99,
                city="Pasto",
                department="Nariño",
                country="Colombia",
            )

    monkeypatch.setenv("AI_ALLOW_LOCATION_DATA_SHARING", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_address_ai_advisor] = lambda: FakeAdvisor()
    try:
        response = await client.post(
            "/api/pedidos",
            headers={"Cookie": _cookie("dispatcher")},
            json={
                "address": "Calle 100",
                "lat": 1.2136,
                "lng": -77.2811,
                "orderId": "AI-OUTSIDE",
            },
        )
    finally:
        app.dependency_overrides.pop(get_address_ai_advisor, None)
        get_settings.cache_clear()

    payload = response.json()
    assert response.status_code == 201
    assert payload["deliveryPoint"]["address"] == "Calle 100"
    assert payload["addressNormalization"]["source"] == "deterministic"
    assert "no confirmó" in payload["addressNormalization"]["warning"]
