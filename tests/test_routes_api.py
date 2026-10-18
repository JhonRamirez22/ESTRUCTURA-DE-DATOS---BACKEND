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
        f"/api/rutas/{route_id}/recalcular",
        headers={"Cookie": _cookie("dispatcher")},
        json={"deliveryPointIds": [str(second_id)]},
    )

    assert recalculated.status_code == 200, recalculated.text
    assert recalculated.json()["recalculated"] is True
    assert len(recalculated.json()["orderedDeliveryPointIds"]) == 2
    async with session_factory() as session:
        points = list(
            (
                await session.scalars(
                    select(DeliveryPoint).where(DeliveryPoint.id.in_([first_id, second_id]))
                )
            ).all()
        )
        assert all(point.route_id == UUID(route_id) for point in points)
        assert {point.sequence_index for point in points} == {0, 1}


@pytest.mark.asyncio
async def test_dispatcher_can_undo_recalculations_in_persisted_lifo_order(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    first_id = await _create_point(session_factory)
    added_id = await _create_point(session_factory, address="Calle 18 # 20-30")
    dispatcher = {"Cookie": _cookie("dispatcher")}
    created = await client.post(
        "/api/rutas",
        headers=dispatcher,
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(first_id)]},
    )
    assert created.status_code == 201, created.text
    route_id = created.json()["route"]["id"]
    initial_route = created.json()["route"]

    first_recalculation = await client.post(
        f"/api/rutas/{route_id}/recalcular",
        headers=dispatcher,
        json={},
    )
    assert first_recalculation.status_code == 200, first_recalculation.text
    assert first_recalculation.json()["route"]["canUndo"] is True
    first_revision_route = first_recalculation.json()["route"]

    second_recalculation = await client.post(
        f"/api/rutas/{route_id}/recalcular",
        headers=dispatcher,
        json={"deliveryPointIds": [str(added_id)]},
    )
    assert second_recalculation.status_code == 200, second_recalculation.text
    assert len(second_recalculation.json()["route"]["deliveryPoints"]) == 2

    listed = await client.get("/api/rutas", headers=dispatcher)
    assert listed.json()["routes"][0]["canUndo"] is True

    undo_latest = await client.post(f"/api/rutas/{route_id}/deshacer-recalculo", headers=dispatcher)
    assert undo_latest.status_code == 200, undo_latest.text
    assert undo_latest.json()["undone"] is True
    assert undo_latest.json()["canUndo"] is True
    assert undo_latest.json()["releasedDeliveryPointIds"] == [str(added_id)]
    assert undo_latest.json()["route"]["geometry"] == first_revision_route["geometry"]
    assert [point["id"] for point in undo_latest.json()["route"]["deliveryPoints"]] == [
        str(first_id)
    ]

    undo_previous = await client.post(
        f"/api/rutas/{route_id}/deshacer-recalculo",
        headers=dispatcher,
    )
    assert undo_previous.status_code == 200, undo_previous.text
    assert undo_previous.json()["canUndo"] is False
    assert undo_previous.json()["route"]["estimatedDurationMinutes"] == initial_route[
        "estimatedDurationMinutes"
    ]
    assert undo_previous.json()["route"]["geometry"] == initial_route["geometry"]

    async with session_factory() as session:
        restored_point = await session.get(DeliveryPoint, first_id)
        released_point = await session.get(DeliveryPoint, added_id)
        assert restored_point is not None
        assert restored_point.route_id == UUID(route_id)
        assert restored_point.sequence_index == 0
        assert released_point is not None
        assert released_point.route_id is None
        assert released_point.sequence_index is None
        assert released_point.status is DeliveryPointStatus.PENDING


@pytest.mark.asyncio
async def test_route_recalculation_undo_requires_dispatcher_and_safe_active_stops(
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

    no_history = await client.post(
        f"/api/rutas/{route_id}/deshacer-recalculo",
        headers={"Cookie": _cookie("dispatcher")},
    )
    denied = await client.post(
        f"/api/rutas/{route_id}/deshacer-recalculo",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
    )
    assert no_history.status_code == 409
    assert denied.status_code == 403

    recalculated = await client.post(
        f"/api/rutas/{route_id}/recalcular",
        headers={"Cookie": _cookie("dispatcher")},
        json={},
    )
    assert recalculated.status_code == 200, recalculated.text
    async with session_factory() as session:
        point = await session.get(DeliveryPoint, point_id)
        assert point is not None
        point.status = DeliveryPointStatus.DELIVERED
        await session.commit()

    unsafe_undo = await client.post(
        f"/api/rutas/{route_id}/deshacer-recalculo",
        headers={"Cookie": _cookie("dispatcher")},
    )
    assert unsafe_undo.status_code == 409
    assert "cambió de estado" in unsafe_undo.json()["error"]


@pytest.mark.asyncio
async def test_route_recalculation_preserves_completed_stop_history(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    first_id = await _create_point(session_factory)
    second_id = await _create_point(session_factory, address="Calle 18 # 20-30")
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(first_id), str(second_id)]},
    )
    assert created.status_code == 201, created.text
    route_id = created.json()["route"]["id"]
    completed_id = UUID(created.json()["orderedDeliveryPointIds"][0])
    active_id = second_id if completed_id == first_id else first_id
    added_id = await _create_point(session_factory, address="Carrera 12 # 7-20")

    started = await client.patch(
        f"/api/rutas/{route_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
        json={"status": "IN_PROGRESS"},
    )
    assert started.status_code == 200, started.text
    delivered = await client.patch(
        f"/api/pedidos/{completed_id}",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
        json={"status": "DELIVERED"},
    )
    assert delivered.status_code == 200, delivered.text

    recalculated = await client.post(
        f"/api/rutas/{route_id}/recalcular",
        headers={"Cookie": _cookie("dispatcher")},
        json={"deliveryPointIds": [str(added_id)]},
    )

    assert recalculated.status_code == 200, recalculated.text
    async with session_factory() as session:
        points = list(
            (
                await session.scalars(
                    select(DeliveryPoint).where(
                        DeliveryPoint.id.in_([completed_id, active_id, added_id])
                    )
                )
            ).all()
        )
        by_id = {point.id: point for point in points}
        completed_point = by_id[completed_id]
        assert completed_point.status is DeliveryPointStatus.DELIVERED
        assert completed_point.route_id == UUID(route_id)
        assert completed_point.sequence_index == 0
        assert by_id[active_id].route_id == UUID(route_id)
        assert by_id[added_id].route_id == UUID(route_id)
        assert {by_id[active_id].sequence_index, by_id[added_id].sequence_index} == {1, 2}


@pytest.mark.asyncio
async def test_route_creation_rejects_coordinates_changed_after_optimization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    plan = OptimizedRoutePlan(
        courier_id=_COURIER_ID,
        planned_courier_location=(-77.2811, 1.2136),
        ordered_delivery_point_ids=(point_id,),
        planned_delivery_point_coordinates=((point_id, (-77.28, 1.214)),),
        estimated_duration_minutes=7.0,
        estimated_distance_meters=800.0,
        baseline_duration_minutes=7.0,
        baseline_distance_meters=800.0,
        geometry={
            "type": "LineString",
            "coordinates": [(-77.2811, 1.2136), (-77.28, 1.214)],
        },
        geometry_provider=RouteGeometryProvider.OSRM,
        matrix_source="osrm",
        warning=None,
        traffic_ai={"source": "deterministic", "applied": False},
    )

    # Simula que el pedido se corrige mientras el proveedor de calles calcula la ruta.
    async with session_factory() as session:
        point = await session.get(DeliveryPoint, point_id)
        assert point is not None
        point.lat = 1.215
        point.lng = -77.279
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(RouteConflictError, match="cambió mientras se guardaba"):
            await create_optimized_routes_batch(session, [plan])

    async with session_factory() as session:
        routes = list(
            await session.scalars(select(Route).where(Route.courier_id == _COURIER_ID))
        )
        point = await session.get(DeliveryPoint, point_id)
        assert routes == []
        assert point is not None
        assert point.route_id is None
        assert point.sequence_index is None


@pytest.mark.asyncio
async def test_route_creation_rejects_courier_moved_after_optimization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    point_id = await _create_point(session_factory)
    plan = OptimizedRoutePlan(
        courier_id=_COURIER_ID,
        planned_courier_location=(-77.2811, 1.2136),
        ordered_delivery_point_ids=(point_id,),
        planned_delivery_point_coordinates=((point_id, (-77.28, 1.214)),),
        estimated_duration_minutes=7.0,
        estimated_distance_meters=800.0,
        baseline_duration_minutes=7.0,
        baseline_distance_meters=800.0,
        geometry={
            "type": "LineString",
            "coordinates": [(-77.2811, 1.2136), (-77.28, 1.214)],
        },
        geometry_provider=RouteGeometryProvider.OSRM,
        matrix_source="osrm",
        warning=None,
        traffic_ai={"source": "deterministic", "applied": False},
    )

    async with session_factory() as session:
        courier = await session.get(Courier, _COURIER_ID)
        assert courier is not None
        courier.current_lat = 1.22
        courier.current_lng = -77.27
        courier.last_location_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(RouteConflictError, match="ubicación del repartidor cambió"):
            await create_optimized_routes_batch(session, [plan])

    async with session_factory() as session:
        assert list(await session.scalars(select(Route))) == []
        point = await session.get(DeliveryPoint, point_id)
        assert point is not None
        assert point.route_id is None
        assert point.sequence_index is None


@pytest.mark.asyncio
async def test_route_recalculation_rejects_a_stop_added_during_routing(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_courier(session_factory)
    first_id = await _create_point(session_factory)
    created = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": str(_COURIER_ID), "deliveryPointIds": [str(first_id)]},
    )
    assert created.status_code == 201, created.text
    route_id = UUID(created.json()["route"]["id"])
    newly_added_id = await _create_point(session_factory, address="Calle 18 # 20-30")

    # Simula otra operación que agrega una parada después de construir el plan vial.
    async with session_factory() as session:
        added_point = await session.get(DeliveryPoint, newly_added_id)
        assert added_point is not None
        added_point.route_id = route_id
        added_point.sequence_index = 1
        await session.commit()

    plan = OptimizedRoutePlan(
        courier_id=_COURIER_ID,
        planned_courier_location=(-77.2811, 1.2136),
        ordered_delivery_point_ids=(first_id,),
        planned_delivery_point_coordinates=((first_id, (-77.28, 1.214)),),
        estimated_duration_minutes=7.0,
        estimated_distance_meters=800.0,
        baseline_duration_minutes=7.0,
        baseline_distance_meters=800.0,
        geometry={
            "type": "LineString",
            "coordinates": [(-77.2811, 1.2136), (-77.28, 1.214)],
        },
        geometry_provider=RouteGeometryProvider.OSRM,
        matrix_source="osrm",
        warning=None,
        traffic_ai={"source": "deterministic", "applied": False},
    )

    async with session_factory() as session:
        with pytest.raises(RouteConflictError, match="Un pedido cambió"):
            await recalculate_route(session, route_id, plan, [first_id])

    async with session_factory() as session:
        added_point = await session.get(DeliveryPoint, newly_added_id)
        assert added_point is not None
        assert added_point.route_id == route_id
        assert added_point.sequence_index == 1


@pytest.mark.asyncio
async def test_route_recalculation_rejects_courier_moved_after_optimization(
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
    assert created.status_code == 201, created.text
    route_id = UUID(created.json()["route"]["id"])
    plan = OptimizedRoutePlan(
        courier_id=_COURIER_ID,
        planned_courier_location=(-77.2811, 1.2136),
        ordered_delivery_point_ids=(point_id,),
        planned_delivery_point_coordinates=((point_id, (-77.28, 1.214)),),
        estimated_duration_minutes=7.0,
        estimated_distance_meters=800.0,
        baseline_duration_minutes=7.0,
        baseline_distance_meters=800.0,
        geometry={
            "type": "LineString",
            "coordinates": [(-77.2811, 1.2136), (-77.28, 1.214)],
        },
        geometry_provider=RouteGeometryProvider.OSRM,
        matrix_source="osrm",
        warning=None,
        traffic_ai={"source": "deterministic", "applied": False},
    )

    async with session_factory() as session:
        courier = await session.get(Courier, _COURIER_ID)
        assert courier is not None
        courier.current_lat = 1.22
        courier.current_lng = -77.27
        courier.last_location_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(RouteConflictError, match="ubicación del repartidor cambió"):
            await recalculate_route(session, route_id, plan, [point_id])

    async with session_factory() as session:
        route = await session.get(Route, route_id)
        point = await session.get(DeliveryPoint, point_id)
        assert route is not None
        assert route.estimated_duration_minutes == created.json()["route"][
            "estimatedDurationMinutes"
        ]
        assert route.geometry == created.json()["route"]["geometry"]
        assert point is not None
        assert point.route_id == route_id
        assert point.sequence_index == 0


@pytest.mark.asyncio
async def test_navigation_and_eta_use_verified_road_geometry_and_matrix_minutes(
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
    cookie = {"Cookie": _cookie("courier", _COURIER_ID)}

    navigation = await client.get(f"/api/rutas/{route_id}/navegacion", headers=cookie)
    assert navigation.status_code == 200, navigation.text
    navigation_payload = navigation.json()["navigation"]
    assert navigation_payload["geometryProvider"] == "OSRM"
    assert navigation_payload["nextDeliveryPointId"] == str(point_id)

    eta = await client.get(f"/api/rutas/{route_id}/eta", headers=cookie)
    assert eta.status_code == 200, eta.text
    eta_payload = eta.json()
    assert eta_payload["approximate"] is True
    assert eta_payload["matrixSource"] == "osrm"
    assert eta_payload["etas"] == [
        {
            "stopId": str(point_id),
            "distanceFromCourierMeters": 501.0,
            "estimatedMinutesFromNow": 6.0,
        }
    ]


@pytest.mark.asyncio
async def test_route_traffic_profile_only_uses_events_during_that_route(
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
    route_id = UUID(created.json()["route"]["id"])
    started_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    async with session_factory() as session:
        route = await session.get(Route, route_id)
        assert route is not None
        route.started_at = started_at
        route.completed_at = started_at + timedelta(minutes=2)
        session.add_all(
            [
                LocationEvent(
                    courier_id=_COURIER_ID,
                    lat=1.2136 + (index * 0.0001),
                    lng=-77.2811 + (index * 0.0003),
                    recorded_at=started_at + timedelta(seconds=30 * index),
                )
                for index in range(3)
            ]
        )
        await session.commit()

    response = await client.get(
        f"/api/rutas/{route_id}/trafico",
        headers={"Cookie": _cookie("dispatcher")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["eventCount"] == 3
    assert response.json()["segmentCount"] == 1
    assert response.json()["sampleCount"] == 1
    assert response.json()["factors"][0]["factor"] == 1


@pytest.mark.asyncio
async def test_route_write_requires_dispatcher_and_rejects_malformed_body(
    client: httpx.AsyncClient,
) -> None:
    denied = await client.post("/api/rutas", json={})
    assert denied.status_code == 401
    malformed = await client.post(
        "/api/rutas",
        headers={"Cookie": _cookie("dispatcher")},
        json={"courierId": "not-a-uuid", "deliveryPointIds": []},
    )
    assert malformed.status_code == 400
