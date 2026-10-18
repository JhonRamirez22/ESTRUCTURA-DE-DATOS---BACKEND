from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    Route,
    RouteStatus,
)
from app.services.route_planner import get_routing_providers
from app.services.traffic import get_traffic_service

_COURIER_ID = UUID("b14a9c80-e0ae-4f7f-a286-1f5622fcacb3")


class FakeMatrixProvider:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls = 0

    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> dict[str, Any]:
        self.calls += 1
        if self.fails:
            raise RuntimeError("matrix unavailable")
        size = len(coordinates)
        durations = [[0.0 if i == j else 8.0 for j in range(size)] for i in range(size)]
        distances = [[0.0 if i == j else 1_200.0 for j in range(size)] for i in range(size)]
        if size >= 3:
            durations[1][2] = 6.0
            distances[1][2] = 700.0
        return {
            "durations_minutes": durations,
            "distances_meters": distances,
            "source": "osrm",
        }


class FakeRouteProvider:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list[list[Coordinate]] = []

    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        self.calls.append(list(coordinates))
        if self.fails:
            raise RuntimeError("route unavailable")
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 1_450,
            "duration_minutes": 9.0,
        }


class FakeTrafficProvider:
    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData:
        del coordinate
        return TrafficData(
            current_speed_kmh=20,
            free_flow_speed_kmh=40,
            current_travel_time_seconds=90,
            free_flow_travel_time_seconds=60,
            confidence=0.9,
            road_closure=False,
            traffic_ratio=0.5,
            traffic_level=TrafficLevel.ALTO,
            travel_time_multiplier=1.5,
            timestamp=datetime.now(UTC),
            cache_status="LIVE",
        )


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tracking.sqlite'}")

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
async def providers() -> AsyncIterator[tuple[FakeMatrixProvider, FakeRouteProvider]]:
    matrix = FakeMatrixProvider()
    route = FakeRouteProvider()
    app.dependency_overrides[get_routing_providers] = lambda: (matrix, route)
    app.dependency_overrides[get_traffic_service] = lambda: None
    try:
        yield matrix, route
    finally:
        app.dependency_overrides.pop(get_routing_providers, None)
        app.dependency_overrides.pop(get_traffic_service, None)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_traffic_service] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_traffic_service, None)


@pytest.mark.asyncio
async def test_tracking_validates_missing_and_unknown_guides(
    client: httpx.AsyncClient,
    providers: tuple[FakeMatrixProvider, FakeRouteProvider],
) -> None:
    missing = await client.get("/api/seguimiento")
    unknown = await client.get("/api/seguimiento?guia=not-a-real-guide")

    assert missing.status_code == 400
    assert missing.json()["error"] == "Ingresa el número de guía para consultar el pedido."
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "No encontramos un pedido con esa guía."
    assert "no-store" in missing.headers["cache-control"]
    assert providers[0].calls == 0


@pytest.mark.asyncio
async def test_unassigned_delivery_point_returns_only_guide_and_destination(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    providers: tuple[FakeMatrixProvider, FakeRouteProvider],
) -> None:
    point = await _create_delivery_point(session_factory)

    response = await client.get(f"/api/seguimiento?guia={point.tracking_token}")

    assert response.status_code == 200
    assert response.json()["tracking"] == {
        "guide": point.tracking_token,
        "status": "PENDING",
        "address": point.address,
        "coordinates": {"lat": point.lat, "lng": point.lng},
        "route": None,
    }
    assert providers[0].calls == 0
    assert providers[1].calls == []


@pytest.mark.asyncio
async def test_active_tracking_shares_rounded_location_and_only_target_leg(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    providers: tuple[FakeMatrixProvider, FakeRouteProvider],
) -> None:
    courier, route, target, private_point = await _create_active_route(session_factory)

    response = await client.get(f"/api/seguimiento?guia={target.tracking_token}")
    payload = response.json()
    tracking = payload["tracking"]
    route_payload = tracking["route"]

    assert response.status_code == 200
    assert route_payload["courier"]["currentLat"] == 1.2136
    assert route_payload["courier"]["currentLng"] == -77.2811
    assert route_payload["estimatedMinutesFromNow"] == 9
    assert route_payload["distanceFromCourierMeters"] == 1_450
    assert route_payload["geometryProvider"] == "OSRM"
    assert providers[0].calls == 1
    assert providers[1].calls == [[(-77.2811, 1.2136), (target.lng, target.lat)]]
    assert route.id.hex not in response.text
    assert courier.name not in response.text and courier.phone not in response.text
    assert "cliente-privado@example.com" not in response.text
    assert "+573009876543" not in response.text
    assert private_point.address not in response.text
    assert str(courier.id) not in response.text
    assert route_payload.get("estimateWarning") is None
    assert route_payload["liveTraffic"] == {
        "source": "ROAD_BASELINE",
        "configured": False,
        "applied": False,
        "status": "NOT_CONFIGURED",
        "scope": "FIRST_LEG_ONLY",
        "warning": "TomTom no está configurado; el ETA usa tiempos viales base.",
    }
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_tracking_applies_partial_tomtom_factor_only_to_the_first_road_leg(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    providers: tuple[FakeMatrixProvider, FakeRouteProvider],
) -> None:
    _courier, _route, target, _private_point = await _create_active_route(session_factory)
    traffic = FakeTrafficProvider()
    app.dependency_overrides[get_traffic_service] = lambda: traffic

    response = await client.get(f"/api/seguimiento?guia={target.tracking_token}")
    payload = response.json()["tracking"]["route"]

    assert response.status_code == 200
    assert payload["estimatedMinutesFromNow"] == 13.5
    assert payload["liveTraffic"]["source"] == "TOMTOM"
    assert payload["liveTraffic"]["applied"] is True
    assert payload["liveTraffic"]["status"] == "LIVE"
    assert "solo el siguiente tramo" in payload["liveTraffic"]["warning"]
    assert providers[1].calls == [[(-77.2811, 1.2136), (target.lng, target.lat)]]


@pytest.mark.asyncio
async def test_stale_location_and_delivered_point_never_expose_courier_or_route(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    providers: tuple[FakeMatrixProvider, FakeRouteProvider],
) -> None:
    courier, _route, target, _private_point = await _create_active_route(
        session_factory,
        location_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
    )

    stale = await client.get(f"/api/seguimiento?guia={target.tracking_token}")
    stale_route = stale.json()["tracking"]["route"]
    assert stale_route["courier"]["currentLat"] is None
    assert stale_route["geometry"] is None
    assert "ubicación reciente" in stale_route["navigationWarning"]
    assert providers[0].calls == 0
    assert providers[1].calls == []

    async with session_factory() as session:
        point = await session.get(DeliveryPoint, target.id)
        assert point is not None
        point.status = DeliveryPointStatus.DELIVERED
        await session.commit()

    delivered = await client.get(f"/api/seguimiento?guia={target.tracking_token}")
    delivered_route = delivered.json()["tracking"]["route"]
    assert delivered_route["courier"]["currentLat"] is None
    assert delivered_route["geometry"] is None
    assert delivered_route["estimatedMinutesFromNow"] is None
    assert str(courier.id) not in delivered.text
    assert providers[0].calls == 0
    assert providers[1].calls == []


@pytest.mark.asyncio
async def test_routing_failures_return_warnings_without_straight_line_geometry(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier, route, target, _private_point = await _create_active_route(session_factory)
    del courier, route
    matrix = FakeMatrixProvider(fails=True)
    road_route = FakeRouteProvider(fails=True)
    app.dependency_overrides[get_routing_providers] = lambda: (matrix, road_route)
    try:
        response = await client.get(f"/api/seguimiento?guia={target.tracking_token}")
    finally:
        app.dependency_overrides.pop(get_routing_providers, None)

    tracking_route = response.json()["tracking"]["route"]
    assert response.status_code == 200
    assert tracking_route["geometry"] is None
    assert "no se muestra una línea recta" in tracking_route["navigationWarning"].lower()
    assert "no se estimó con distancias en línea recta" in tracking_route["estimateWarning"]
    assert tracking_route["estimatedMinutesFromNow"] is None


async def _create_delivery_point(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    address: str = "Carrera 25 #18-20",
) -> DeliveryPoint:
    point = DeliveryPoint(
        id=uuid4(),
        address=address,
        lat=1.215,
        lng=-77.279,
        status=DeliveryPointStatus.PENDING,
        order_id=f"ORDER-{uuid4()}",
        tracking_token=f"TRACK-{uuid4()}",
    )
    async with session_factory() as session:
        session.add(point)
        await session.commit()
        await session.refresh(point)
    return point


async def _create_active_route(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    location_at: datetime | None = None,
) -> tuple[Courier, Route, DeliveryPoint, DeliveryPoint]:
    courier = Courier(
        id=_COURIER_ID,
        name="Nombre de repartidor privado",
        phone="+573001234567",
        status=CourierStatus.ON_ROUTE,
        current_lat=1.213612,
        current_lng=-77.281145,
        last_location_at=location_at or datetime.now(UTC).replace(tzinfo=None),
    )
    route = Route(
        id=uuid4(),
        courier_id=courier.id,
        status=RouteStatus.IN_PROGRESS,
        estimated_duration_minutes=16,
        estimated_distance_meters=2_000,
    )
    target = DeliveryPoint(
        id=uuid4(),
        address="Destino que corresponde a la guía",
        lat=1.215,
        lng=-77.279,
        status=DeliveryPointStatus.EN_ROUTE,
        order_id=f"ORDER-{uuid4()}",
        tracking_token=f"TRACK-{uuid4()}",
        customer_email="cliente-privado@example.com",
        email_notifications_enabled=True,
        customer_phone="+573009876543",
        sms_notifications_enabled=True,
        route_id=route.id,
        sequence_index=0,
    )
    private_point = DeliveryPoint(
        id=uuid4(),
        address="Otra dirección privada que no se debe revelar",
        lat=1.216,
        lng=-77.276,
        status=DeliveryPointStatus.PENDING,
        order_id=f"ORDER-{uuid4()}",
        tracking_token=f"TRACK-{uuid4()}",
        route_id=route.id,
        sequence_index=1,
    )
    async with session_factory() as session:
        session.add_all([courier, route, target, private_point])
        await session.commit()
    return courier, route, target, private_point
