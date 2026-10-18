from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from time import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.core.ai.route_traffic import RouteTrafficAdjustment, RouteTrafficAdvice
from app.core.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
)
from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteGeometryProvider,
    RouteStatus,
)
from app.services.route_planner import (
    OptimizedRoutePlan,
    RouteConflictError,
    create_optimized_routes_batch,
    get_routing_providers,
    recalculate_route,
)
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = UUID("f8fbc95f-643e-4fb3-9b82-8c42e0707311")


class FakeMatrixProvider:
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        size = len(coordinates)
        durations: list[list[float | None]] = [
            [0.0 if row == column else 5.0 + row + column for column in range(size)]
            for row in range(size)
        ]
        distances: list[list[float | None]] = [
            [0.0 if row == column else 500.0 + row + column for column in range(size)]
            for row in range(size)
        ]
        if size >= 3:
            durations[0][1] = 8
            durations[0][2] = 2
            durations[2][1] = 1
            durations[1][2] = 1
        return {
            "durations_minutes": durations,
            "distances_meters": distances,
            "source": "osrm",
        }


class FakeRouteProvider:
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 2_100,
            "duration_minutes": 12.5,
        }


class FakeOrsMatrixProvider(FakeMatrixProvider):
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        result = await super().get_duration_matrix(coordinates)
        result["source"] = "ors"
        result.pop("distances_meters", None)
        return result


class FakeOrsRouteProvider(FakeRouteProvider):
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        result = await super().get_route(coordinates)
        result["geometry_provider"] = "ORS"
        return result


class FakeRouteTrafficAdvisor:
    def __init__(self) -> None:
        self.requests = 0

    async def get_route_advice(self, _request: object) -> RouteTrafficAdvice:
        self.requests += 1
        return RouteTrafficAdvice(
            adjustments=(
                RouteTrafficAdjustment("stop-0", "stop-1", traffic_multiplier=1.4),
            ),
            model="unit-test-model",
        )


class FakeTomTomTrafficProvider:
    def __init__(self) -> None:
        self.coordinates: list[Coordinate] = []

    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData:
        self.coordinates.append(coordinate)
        return TrafficData(
            current_speed_kmh=20,
            free_flow_speed_kmh=40,
            current_travel_time_seconds=120,
            free_flow_travel_time_seconds=60,
            confidence=0.9,
            road_closure=False,
            traffic_ratio=0.5,
            traffic_level=TrafficLevel.ALTO,
            travel_time_multiplier=2.0,
            timestamp=datetime.now(UTC),
        )


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'routes.sqlite'}")

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
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", _SESSION_SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.setenv("AI_ALLOW_LOCATION_DATA_SHARING", "false")
    get_settings.cache_clear()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_routing_providers] = lambda: (
        FakeMatrixProvider(),
        FakeRouteProvider(),
    )
    app.dependency_overrides[get_route_traffic_advisor] = lambda: None
    app.dependency_overrides[get_traffic_service] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_routing_providers, None)
        app.dependency_overrides.pop(get_route_traffic_advisor, None)
        app.dependency_overrides.pop(get_traffic_service, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: UUID | None = None) -> str:
    token = create_session_token(
        role,  # type: ignore[arg-type]
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SESSION_SECRET,
        str(courier_id) if courier_id else None,
    )
    return f"{SESSION_COOKIE_NAME}={token}"


async def _create_courier(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    courier_id: UUID = _COURIER_ID,
    status: CourierStatus = CourierStatus.AVAILABLE,
) -> UUID:
    async with session_factory() as session:
        session.add(
            Courier(
                id=courier_id,
                name="Courier Privado",
                phone="3001112233",
                status=status,
                current_lat=1.2136,
                current_lng=-77.2811,
                last_location_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()
    return courier_id


async def _create_point(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    address: str = "Carrera 25 # 4 sur 65",
) -> UUID:
    point_id = uuid4()
    async with session_factory() as session:
        session.add(
            DeliveryPoint(
                id=point_id,
                address=address,
                lat=1.214,
                lng=-77.28,
                status=DeliveryPointStatus.PENDING,
                order_id=f"ORDER-{point_id}",
                tracking_token=f"TRACK-{point_id}",
            )
        )
        await session.commit()
    return point_id


@pytest.mark.asyncio
async def test_route_creation_optimizes_and_commits_route_and_ordered_points(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    first_id = await _create_point(session_factory, address="Carrera 25 # 4 sur 65")
    second_id = await _create_point(session_factory, address="Calle 18 # 20-30")

    response = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(first_id), str(second_id)]},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["orderedDeliveryPointIds"] == [str(second_id), str(first_id)]
    assert payload["matrixSource"] == "osrm"
    assert payload["route"]["geometryProvider"] == "OSRM"
    assert payload["route"]["estimatedDurationMinutes"] == 12.5
    async with session_factory() as session:
        route = await session.scalar(select(Route).options(selectinload(Route.delivery_points)))
        assert route is not None
        assert route.status == RouteStatus.PLANNED
        ordered = sorted(route.delivery_points, key=lambda point: point.sequence_index or 0)
        assert [point.id for point in ordered] == [second_id, first_id]
        assert [point.sequence_index for point in ordered] == [0, 1]


@pytest.mark.asyncio
async def test_route_creation_applies_tomtom_factor_and_returns_coverage(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    traffic = FakeTomTomTrafficProvider()
    app.dependency_overrides[get_traffic_service] = lambda: traffic

    response = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(point_id)]},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["route"]["estimatedDurationMinutes"] == 12.0
    assert payload["trafficAi"]["source"] == "tomtom-live"
    assert payload["liveTraffic"]["source"] == "tomtom"
    assert payload["liveTraffic"]["adjustedSegments"] == 1
    assert payload["liveTraffic"]["coverageRatio"] == 1.0
    assert len(traffic.coordinates) == 1


@pytest.mark.asyncio
async def test_ors_road_matrix_can_use_traffic_advice_without_fabricated_distance_data(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    advisor = FakeRouteTrafficAdvisor()
    app.dependency_overrides[get_routing_providers] = lambda: (
        FakeOrsMatrixProvider(),
        FakeOrsRouteProvider(),
    )
    app.dependency_overrides[get_route_traffic_advisor] = lambda: advisor

    response = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(point_id)]},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["matrixSource"] == "ors"
    assert payload["trafficAi"]["source"] == "external-ai"
    assert payload["trafficAi"]["applied"] is True
    assert advisor.requests == 1
    assert payload["route"]["baselineDurationMinutes"] == 6.0
    assert payload["route"]["baselineDistanceMeters"] is None
    assert payload["route"]["geometryProvider"] == "ORS"


@pytest.mark.asyncio
async def test_courier_route_response_is_scoped_and_redacts_dispatcher_and_customer_secrets(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(point_id)]},
    )
    route_id = created.json()["route"]["id"]

    own_route = await client.get(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
    )
    assert own_route.status_code == 200
    serialized = own_route.text
    assert "Courier Privado" not in serialized
    assert "3001112233" not in serialized
    assert "ORDER-" not in serialized
    assert "TRACK-" not in serialized

    other_courier_id = UUID("9df3a049-262e-4c14-a185-46a210cbb5ee")
    other_route = await client.get(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("courier", other_courier_id)},
    )
    assert other_route.status_code == 404


@pytest.mark.asyncio
async def test_route_lifecycle_updates_points_and_courier_atomically(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(point_id)]},
    )
    route_id = created.json()["route"]["id"]

    started = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
        json={"status": "IN_PROGRESS"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["route"]["status"] == "IN_PROGRESS"
    async with session_factory() as session:
        courier = await session.get(Courier, _COURIER_ID)
        point = await session.get(DeliveryPoint, point_id)
        assert courier is not None and courier.status == CourierStatus.ON_ROUTE
        assert point is not None and point.status == DeliveryPointStatus.EN_ROUTE

    refused = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("dispatcher")},
        json={"status": "COMPLETED"},
    )
    assert refused.status_code == 409

    async with session_factory() as session:
        point = await session.get(DeliveryPoint, point_id)
        assert point is not None
        point.status = DeliveryPointStatus.DELIVERED
        await session.commit()
    completed = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("dispatcher")},
        json={"status": "COMPLETED"},
    )
    assert completed.status_code == 200
    async with session_factory() as session:
        route = await session.get(Route, UUID(route_id))
        courier = await session.get(Courier, _COURIER_ID)
        assert route is not None and route.completed_at is not None
        assert courier is not None and courier.status == CourierStatus.AVAILABLE


@pytest.mark.asyncio
async def test_cancelling_route_keeps_delivered_stops_and_requeues_only_active_ones(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_ids = [
        await _create_point(session_factory),
        await _create_point(session_factory, address="Calle 18 # 20-30"),
    ]
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={
            "courierId": str(_COURIER_ID),
            "deliveryPointIds": [str(point) for point in point_ids],
        },
    )
    assert created.status_code == 201, created.text
    route_id = created.json()["route"]["id"]
    delivered_id = UUID(created.json()["orderedDeliveryPointIds"][0])
    active_id = next(point_id for point_id in point_ids if point_id != delivered_id)

    started = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
        json={"status": "IN_PROGRESS"},
    )
    assert started.status_code == 200, started.text
    delivered = await client.patch(
        f"/api/pedidos/{delivered_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
        json={"status": "DELIVERED"},
    )
    assert delivered.status_code == 200, delivered.text

    cancelled = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("dispatcher")},
        json={"status": "CANCELLED"},
    )

    assert cancelled.status_code == 200, cancelled.text
    async with session_factory() as session:
        delivered_point = await session.get(DeliveryPoint, delivered_id)
        active_point = await session.get(DeliveryPoint, active_id)
        courier = await session.get(Courier, _COURIER_ID)
        route = await session.get(Route, UUID(route_id))
        assert delivered_point is not None
        assert delivered_point.status is DeliveryPointStatus.DELIVERED
        assert delivered_point.route_id == UUID(route_id)
        assert active_point is not None
        assert active_point.status is DeliveryPointStatus.PENDING
        assert active_point.route_id is None
        assert active_point.sequence_index is None
        assert courier is not None and courier.status is CourierStatus.AVAILABLE
        assert route is not None and route.status is RouteStatus.CANCELLED


@pytest.mark.asyncio
async def test_route_recalculation_adds_pending_stop_without_partial_assignment(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    first_id = await _create_point(session_factory)
    second_id = await _create_point(session_factory, address="Calle 18 # 20-30")
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(first_id)]},
    )
    route_id = created.json()["route"]["id"]

    recalculated = await client.post(
