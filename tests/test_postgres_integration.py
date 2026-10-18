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
