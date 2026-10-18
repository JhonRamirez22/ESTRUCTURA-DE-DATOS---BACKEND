from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_token
from app.core.routing.cached_matrix_provider import MatrixProviderResult
from app.core.routing.contracts import Coordinate, RouteResult
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
    RouteGeometryProvider,
    RouteRevision,
    RouteStatus,
)
from app.repositories.customer_notification_repository import CustomerNotificationRepository
from app.services.customer_notifications import (
    enqueue_courier_near_notification,
    enqueue_order_assigned_notification,
)
from app.services.route_planner import (
    OptimizedRoutePlan,
    RouteConflictError,
    create_optimized_routes_batch,
    get_routing_providers,
    recalculate_route,
    undo_route_recalculation,
    update_route_status,
)
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_INTEGRATION_SECRET = "postgres-integration-secret-with-at-least-32-bytes"
_COURIER_ID = uuid4()
_POINT_ID = uuid4()
_COMPETING_COURIER_ID = uuid4()
_FIRST_POINT_ID = uuid4()
_CONFLICTING_POINT_ID = uuid4()
_EXISTING_ROUTE_ID = uuid4()
_CANCELLATION_COURIER_ID = uuid4()
_CANCELLATION_ROUTE_ID = uuid4()
_DELIVERED_POINT_ID = uuid4()
_ACTIVE_POINT_ID = uuid4()


class _MatrixProvider:
    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> MatrixProviderResult:
        size = len(coordinates)
        return {
            "durations_minutes": [
                [0.0 if row == column else 4.5 for column in range(size)]
                for row in range(size)
            ],
            "distances_meters": [
                [0.0 if row == column else 650.0 for column in range(size)]
                for row in range(size)
            ],
            "source": "osrm",
        }


class _RouteProvider:
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 650.0,
            "duration_minutes": 4.5,
        }


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)
@pytest.mark.asyncio
async def test_route_revision_stack_persists_and_undoes_atomically_in_postgres() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    courier_id = uuid4()
    route_id = uuid4()
    original_point_id = uuid4()
    appended_point_id = uuid4()
    seeded = False
    original_geometry = {
        "type": "LineString",
        "coordinates": [[-77.2811, 1.2136], [-77.28, 1.214]],
    }

    try:
        async with session_factory() as session:
            session.add(
                Courier(
                    id=courier_id,
                    name="Courier historial PG",
                    phone=f"test-{courier_id.hex[:20]}",
                    status=CourierStatus.AVAILABLE,
                    current_lat=1.2136,
                    current_lng=-77.2811,
                    last_location_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await session.flush()
            session.add(
                Route(
                    id=route_id,
                    courier_id=courier_id,
                    status=RouteStatus.PLANNED,
                    estimated_duration_minutes=15.0,
                    estimated_distance_meters=1_800.0,
                    baseline_duration_minutes=14.0,
                    baseline_distance_meters=1_700.0,
                    geometry=original_geometry,
                    geometry_provider=RouteGeometryProvider.OSRM,
                )
            )
            session.add_all(
                [
                    DeliveryPoint(
                        id=original_point_id,
                        address="Carrera 25 # 4 Sur-65, Pasto",
                        lat=1.214,
                        lng=-77.28,
                        status=DeliveryPointStatus.PENDING,
                        order_id=f"REVISION-{original_point_id}",
                        tracking_token=f"TRACK-{original_point_id}",
                        route_id=route_id,
                        sequence_index=0,
                    ),
                    DeliveryPoint(
                        id=appended_point_id,
                        address="Calle 18 # 20-30, Pasto",
                        lat=1.215,
                        lng=-77.279,
                        status=DeliveryPointStatus.PENDING,
                        order_id=f"REVISION-{appended_point_id}",
                        tracking_token=f"TRACK-{appended_point_id}",
                    ),
                ]
            )
            await session.commit()
            seeded = True

        plan = OptimizedRoutePlan(
            courier_id=courier_id,
            planned_courier_location=(-77.2811, 1.2136),
            ordered_delivery_point_ids=(appended_point_id, original_point_id),
            planned_delivery_point_coordinates=(
                (original_point_id, (-77.28, 1.214)),
                (appended_point_id, (-77.279, 1.215)),
            ),
            estimated_duration_minutes=11.0,
            estimated_distance_meters=1_250.0,
            baseline_duration_minutes=12.0,
            baseline_distance_meters=1_400.0,
            geometry={
                "type": "LineString",
                "coordinates": [[-77.2811, 1.2136], [-77.279, 1.215], [-77.28, 1.214]],
            },
            geometry_provider=RouteGeometryProvider.OSRM,
            matrix_source="osrm",
            warning=None,
            traffic_ai={"source": "deterministic", "applied": False},
        )
        async with session_factory() as session:
            await recalculate_route(session, route_id, plan, [original_point_id])

        async with session_factory() as session:
            revisions = list(
                (
                    await session.scalars(
                        select(RouteRevision).where(RouteRevision.route_id == route_id)
                    )
                ).all()
            )
            assert len(revisions) == 1
            assert revisions[0].snapshot["estimatedDurationMinutes"] == 15.0

        async with session_factory() as session:
            result = await undo_route_recalculation(session, route_id)
            assert result.can_undo is False
            assert result.released_delivery_point_ids == (appended_point_id,)
            assert result.route.estimated_duration_minutes == 15.0
            assert result.route.geometry == original_geometry

        async with session_factory() as session:
            active = await session.get(DeliveryPoint, original_point_id)
            released = await session.get(DeliveryPoint, appended_point_id)
            remaining_revisions = list(
                (
                    await session.scalars(
                        select(RouteRevision).where(RouteRevision.route_id == route_id)
                    )
                ).all()
            )
            assert active is not None and active.route_id == route_id
            assert active.sequence_index == 0
            assert released is not None
            assert released.route_id is None
            assert released.sequence_index is None
            assert released.status is DeliveryPointStatus.PENDING
            assert remaining_revisions == []
    finally:
        try:
            if seeded:
                async with session_factory() as session:
                    await session.execute(delete(Route).where(Route.id == route_id))
                    await session.execute(
                        delete(DeliveryPoint).where(
                            DeliveryPoint.id.in_({original_point_id, appended_point_id})
                        )
                    )
                    await session.execute(
                        delete(Notification).where(Notification.recipient_id == str(courier_id))
                    )
                    await session.execute(delete(Courier).where(Courier.id == courier_id))
                    await session.commit()
        finally:
            await engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)
@pytest.mark.asyncio
async def test_route_creation_persists_atomically_in_postgres() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    monkeypatch_values = {
        "AUTH_SECRET": _INTEGRATION_SECRET,
        "NODE_ENV": "test",
        "AI_ALLOW_LOCATION_DATA_SHARING": "false",
    }
    original_values = {name: os.environ.get(name) for name in monkeypatch_values}
    os.environ.update(monkeypatch_values)
    get_settings.cache_clear()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_routing_providers] = lambda: (
        _MatrixProvider(),
        _RouteProvider(),
    )
    app.dependency_overrides[get_route_traffic_advisor] = lambda: None
    app.dependency_overrides[get_traffic_service] = lambda: None
    seeded = False

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

        async with session_factory() as session:
            session.add(
                Courier(
                    id=_COURIER_ID,
                    name="Integración automatizada",
                    phone=f"test-{_COURIER_ID.hex[:20]}",
                    status=CourierStatus.AVAILABLE,
                    current_lat=1.2136,
                    current_lng=-77.2811,
                    last_location_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            session.add(
                DeliveryPoint(
                    id=_POINT_ID,
                    address="Carrera 25 # 4 Sur-65, Pasto",
                    lat=1.214,
                    lng=-77.28,
                    status=DeliveryPointStatus.PENDING,
                    order_id=f"INTEGRATION-{_POINT_ID}",
                    tracking_token=f"INTEGRATION-{_POINT_ID}",
                )
            )
            await session.commit()
            seeded = True

        expires_at = int(datetime.now(UTC).timestamp()) + SESSION_MAX_AGE_SECONDS
        token = create_session_token("dispatcher", expires_at, _INTEGRATION_SECRET)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://postgres-integration",
        ) as client:
            response = await client.post(
                "/api/rutas",
                headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
                json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(_POINT_ID)]},
            )

        assert response.status_code == 201, response.text
        route_id = UUID(response.json()["route"]["id"])
        assert response.json()["orderedDeliveryPointIds"] == [str(_POINT_ID)]

        async with session_factory() as session:
            route = await session.scalar(select(Route).where(Route.id == route_id))
            point = await session.get(DeliveryPoint, _POINT_ID)
            assert route is not None
            assert route.courier_id == _COURIER_ID
            assert point is not None
            assert point.route_id == route_id
            assert point.sequence_index == 0
            assert point.status is DeliveryPointStatus.PENDING
    finally:
        try:
            if seeded:
                async with session_factory() as session:
                    await session.execute(
                        delete(Route).where(Route.courier_id == _COURIER_ID)
                    )
                    await session.execute(
                        delete(DeliveryPoint).where(DeliveryPoint.id == _POINT_ID)
                    )
                    await session.execute(
                        delete(Notification).where(Notification.recipient_id == str(_COURIER_ID))
                    )
                    await session.execute(delete(Courier).where(Courier.id == _COURIER_ID))
                    await session.commit()
        finally:
            app.dependency_overrides.pop(get_session, None)
            app.dependency_overrides.pop(get_routing_providers, None)
            app.dependency_overrides.pop(get_route_traffic_advisor, None)
            app.dependency_overrides.pop(get_traffic_service, None)
            for name, value in original_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            get_settings.cache_clear()
            await engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)
@pytest.mark.asyncio
async def test_route_batch_failure_rolls_back_partial_assignments_in_postgres() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = False

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

        async with session_factory() as session:
            session.add_all(
                [
                    Courier(
                        id=_COURIER_ID,
                        name="Courier transaccional",
                        phone=f"test-{_COURIER_ID.hex[:20]}",
                        status=CourierStatus.AVAILABLE,
                        current_lat=1.2136,
                        current_lng=-77.2811,
                        last_location_at=datetime.now(UTC).replace(tzinfo=None),
                    ),
                    Courier(
                        id=_COMPETING_COURIER_ID,
                        name="Courier con reserva",
                        phone=f"test-{_COMPETING_COURIER_ID.hex[:20]}",
                        status=CourierStatus.AVAILABLE,
                        current_lat=1.2136,
                        current_lng=-77.2811,
                        last_location_at=datetime.now(UTC).replace(tzinfo=None),
                    ),
                ]
            )
            await session.flush()
            session.add(
                Route(
                    id=_EXISTING_ROUTE_ID,
                    courier_id=_COMPETING_COURIER_ID,
                    status=RouteStatus.PLANNED,
                    estimated_duration_minutes=8.0,
                    estimated_distance_meters=900.0,
                )
            )
            await session.flush()
            session.add_all(
                [
                    DeliveryPoint(
                        id=_FIRST_POINT_ID,
                        address="Carrera 25 # 4 Sur-65, Pasto",
                        lat=1.214,
                        lng=-77.28,
                        status=DeliveryPointStatus.PENDING,
                        order_id=f"INTEGRATION-{_FIRST_POINT_ID}",
                        tracking_token=f"INTEGRATION-{_FIRST_POINT_ID}",
                    ),
                    DeliveryPoint(
                        id=_CONFLICTING_POINT_ID,
                        address="Calle 18 # 20-30, Pasto",
                        lat=1.215,
                        lng=-77.279,
                        status=DeliveryPointStatus.PENDING,
                        order_id=f"INTEGRATION-{_CONFLICTING_POINT_ID}",
                        tracking_token=f"INTEGRATION-{_CONFLICTING_POINT_ID}",
                        route_id=_EXISTING_ROUTE_ID,
                        sequence_index=0,
                    ),
                ]
            )
            await session.commit()
            seeded = True

        plan = OptimizedRoutePlan(
            courier_id=_COURIER_ID,
            planned_courier_location=(-77.2811, 1.2136),
            ordered_delivery_point_ids=(_FIRST_POINT_ID, _CONFLICTING_POINT_ID),
            planned_delivery_point_coordinates=(
                (_FIRST_POINT_ID, (-77.28, 1.214)),
                (_CONFLICTING_POINT_ID, (-77.279, 1.215)),
            ),
            estimated_duration_minutes=12.0,
            estimated_distance_meters=1_200.0,
            baseline_duration_minutes=12.0,
            baseline_distance_meters=1_200.0,
            geometry={
                "type": "LineString",
                "coordinates": [(-77.2811, 1.2136), (-77.28, 1.214), (-77.279, 1.215)],
            },
            geometry_provider=RouteGeometryProvider.OSRM,
            matrix_source="osrm",
            warning=None,
            traffic_ai={"source": "deterministic", "applied": False},
        )

        async with session_factory() as session:
            with pytest.raises(RouteConflictError, match="cambió mientras se guardaba"):
                await create_optimized_routes_batch(session, [plan])

        async with session_factory() as session:
            created_routes = await session.scalars(
                select(Route).where(Route.courier_id == _COURIER_ID)
            )
            first_point = await session.get(DeliveryPoint, _FIRST_POINT_ID)
            conflicting_point = await session.get(DeliveryPoint, _CONFLICTING_POINT_ID)
            assert list(created_routes) == []
            assert first_point is not None and first_point.route_id is None
            assert conflicting_point is not None
            assert conflicting_point.route_id == _EXISTING_ROUTE_ID
            assert conflicting_point.sequence_index == 0
    finally:
        try:
            if seeded:
                async with session_factory() as session:
                    await session.execute(
                        delete(Notification).where(
                            Notification.recipient_id.in_(
                                {str(_COURIER_ID), str(_COMPETING_COURIER_ID)}
                            )
                        )
                    )
                    await session.execute(
                        delete(Route).where(Route.id == _EXISTING_ROUTE_ID)
                    )
                    await session.execute(
                        delete(DeliveryPoint).where(
                            DeliveryPoint.id.in_({_FIRST_POINT_ID, _CONFLICTING_POINT_ID})
                        )
                    )
                    await session.execute(
                        delete(Courier).where(
                            Courier.id.in_({_COURIER_ID, _COMPETING_COURIER_ID})
                        )
                    )
                    await session.commit()
        finally:
            await engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)
@pytest.mark.asyncio
async def test_route_cancellation_preserves_delivered_points_in_postgres() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = False

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

        async with session_factory() as session:
            session.add(
                Courier(
                    id=_CANCELLATION_COURIER_ID,
                    name="Courier con entregas",
                    phone=f"test-{_CANCELLATION_COURIER_ID.hex[:20]}",
                    status=CourierStatus.ON_ROUTE,
                    current_lat=1.2136,
                    current_lng=-77.2811,
                    last_location_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await session.flush()
            session.add(
                Route(
                    id=_CANCELLATION_ROUTE_ID,
                    courier_id=_CANCELLATION_COURIER_ID,
                    status=RouteStatus.IN_PROGRESS,
                    estimated_duration_minutes=10.0,
                    estimated_distance_meters=1_000.0,
                )
            )
            await session.flush()
            session.add_all(
                [
                    DeliveryPoint(
                        id=_DELIVERED_POINT_ID,
                        address="Carrera 25 # 4 Sur-65, Pasto",
                        lat=1.214,
                        lng=-77.28,
                        status=DeliveryPointStatus.DELIVERED,
                        order_id=f"INTEGRATION-{_DELIVERED_POINT_ID}",
                        tracking_token=f"INTEGRATION-{_DELIVERED_POINT_ID}",
                        route_id=_CANCELLATION_ROUTE_ID,
                        sequence_index=0,
                    ),
                    DeliveryPoint(
                        id=_ACTIVE_POINT_ID,
                        address="Calle 18 # 20-30, Pasto",
                        lat=1.215,
                        lng=-77.279,
                        status=DeliveryPointStatus.EN_ROUTE,
                        order_id=f"INTEGRATION-{_ACTIVE_POINT_ID}",
                        tracking_token=f"INTEGRATION-{_ACTIVE_POINT_ID}",
                        route_id=_CANCELLATION_ROUTE_ID,
                        sequence_index=1,
                    ),
                ]
            )
            await session.commit()
            seeded = True

        async with session_factory() as session:
            await update_route_status(session, _CANCELLATION_ROUTE_ID, RouteStatus.CANCELLED)

        async with session_factory() as session:
            route = await session.get(Route, _CANCELLATION_ROUTE_ID)
            delivered_point = await session.get(DeliveryPoint, _DELIVERED_POINT_ID)
            active_point = await session.get(DeliveryPoint, _ACTIVE_POINT_ID)
            courier = await session.get(Courier, _CANCELLATION_COURIER_ID)
            assert route is not None and route.status is RouteStatus.CANCELLED
            assert delivered_point is not None
            assert delivered_point.status is DeliveryPointStatus.DELIVERED
            assert delivered_point.route_id == _CANCELLATION_ROUTE_ID
            assert delivered_point.sequence_index == 0
            assert active_point is not None
            assert active_point.status is DeliveryPointStatus.PENDING
            assert active_point.route_id is None
            assert active_point.sequence_index is None
            assert courier is not None and courier.status is CourierStatus.AVAILABLE
    finally:
        try:
            if seeded:
                async with session_factory() as session:
                    await session.execute(
                        delete(Route).where(Route.id == _CANCELLATION_ROUTE_ID)
                    )
                    await session.execute(
                        delete(DeliveryPoint).where(
                            DeliveryPoint.id.in_({_DELIVERED_POINT_ID, _ACTIVE_POINT_ID})
                        )
                    )
                    await session.execute(
                        delete(Courier).where(Courier.id == _CANCELLATION_COURIER_ID)
                    )
                    await session.commit()
        finally:
            await engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)
@pytest.mark.asyncio
async def test_customer_notification_outbox_persists_and_deduplicates_in_postgres() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    point_id = uuid4()
    route_id = uuid4()
    order_reference = f"INTEGRATION-NOTIFICATION-{point_id}"
    seeded = False

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

        async with session_factory() as session:
            session.add(
                DeliveryPoint(
                    id=point_id,
                    address="Carrera 25 # 4 Sur-65, Pasto",
                    lat=1.214,
                    lng=-77.28,
                    status=DeliveryPointStatus.PENDING,
                    order_id=order_reference,
                    tracking_token=f"TRACK-{point_id}",
                    customer_email="integration@example.invalid",
                    email_notifications_enabled=True,
                    customer_phone="+573001234567",
                    sms_notifications_enabled=True,
                )
            )
            await session.commit()
            seeded = True

        async with session_factory() as session:
            point = await session.get(DeliveryPoint, point_id)
            assert point is not None
            expected_channels = (
                CustomerNotificationChannel.EMAIL,
                CustomerNotificationChannel.SMS,
            )
            assert await enqueue_order_assigned_notification(session, point, route_id) == (
                expected_channels
            )
            assert await enqueue_order_assigned_notification(session, point, route_id) == ()
            assert await enqueue_courier_near_notification(session, point, route_id) == (
                expected_channels
            )
            assert await enqueue_courier_near_notification(session, point, route_id) == ()
            await session.commit()

        async with session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(CustomerNotificationOutbox).where(
                            CustomerNotificationOutbox.delivery_point_id == point_id
                        )
                    )
                ).all()
            )
            assert len(rows) == 4
            assert {row.event_type for row in rows} == {
                CustomerNotificationEvent.ORDER_ASSIGNED,
                CustomerNotificationEvent.COURIER_NEAR,
            }
            assert {row.channel for row in rows} == set(expected_channels)
            assert all(row.status is CustomerNotificationStatus.PENDING for row in rows)
            assert all(row.attempt_count == 0 for row in rows)
            assert all("integration@example.invalid" not in row.text_body for row in rows)
            assert all("3001234567" not in row.text_body for row in rows)
            assert all("Carrera 25" not in row.text_body for row in rows)
            counts = await CustomerNotificationRepository(session).active_outbox_counts()
            assert counts[CustomerNotificationChannel.EMAIL]["pending"] == 2
            assert counts[CustomerNotificationChannel.SMS]["pending"] == 2
    finally:
        try:
            if seeded:
                async with session_factory() as session:
                    await session.execute(
                        delete(CustomerNotificationOutbox).where(
                            CustomerNotificationOutbox.delivery_point_id == point_id
                        )
                    )
                    await session.execute(
                        delete(DeliveryPoint).where(DeliveryPoint.id == point_id)
                    )
                    await session.commit()
        finally:
            await engine.dispose()
