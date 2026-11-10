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
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    session_cookie_name,
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
