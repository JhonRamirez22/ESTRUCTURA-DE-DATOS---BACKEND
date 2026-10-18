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


