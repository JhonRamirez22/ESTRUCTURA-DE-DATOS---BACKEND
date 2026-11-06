"""Live fleet re-optimisation endpoint: permissions, proposals, caching and failure modes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection
from time import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import optimizations as optimizations_module
from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.core.location.proximity import haversine_m
from app.core.routing.contracts import Coordinate
from app.core.routing.routing_errors import RoutingClientError
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
from app.services import live_optimization
from app.services.route_planner import get_routing_providers
from app.settings import get_settings

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


class FakeMatrix:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RoutingClientError("osrm down")
        rows = [
            [0.0 if a == b else haversine_m(a[1], a[0], b[1], b[0]) / 400.0 for b in coordinates]
            for a in coordinates
        ]
        return {"durations_minutes": rows, "source": "osrm"}


class FakeRoutes:
    def __init__(self) -> None:
        self.calls = 0

    async def get_route(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        self.calls += 1
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM", "distance_meters": 1000.0, "duration_minutes": 5.0,
        }


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'live.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(connection: Connection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def setup(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeMatrix, FakeRoutes]]:
    monkeypatch.setenv("AUTH_SECRET", _SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    get_settings.cache_clear()
    live_optimization._SHARED_CACHE.clear()
    optimizations_module._LIVE_LIMITER._hits.clear()
    matrix, routes = FakeMatrix(), FakeRoutes()

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_routing_providers] = lambda: (matrix, routes)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, matrix, routes
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_routing_providers, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: UUID | None = None) -> dict[str, str]:
    token = create_session_token(
        role,  # type: ignore[arg-type]
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SECRET,
        str(courier_id) if courier_id else None,
    )
    return {"Cookie": f"{session_cookie_name(role)}={token}"}  # type: ignore[arg-type]


async def _seed_route(
    factory: async_sessionmaker[AsyncSession],
    name: str,
