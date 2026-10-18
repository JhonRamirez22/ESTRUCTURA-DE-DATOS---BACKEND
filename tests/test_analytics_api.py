from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_token
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Courier, CourierStatus, Route, RouteStatus
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = UUID("f8fbc95f-643e-4fb3-9b82-8c42e0707311")


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analytics.sqlite'}")
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
        get_settings.cache_clear()


def _dispatcher_cookie() -> str:
    token = create_session_token(
        "dispatcher",
        int(time.time()) + SESSION_MAX_AGE_SECONDS,
        _SESSION_SECRET,
    )
    return f"{SESSION_COOKIE_NAME}={token}"


@pytest.mark.asyncio
async def test_analytics_requires_dispatcher_session(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/analitica")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_analytics_excludes_cancelled_routes_and_aggregates_metrics(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        courier = Courier(
            id=_COURIER_ID,
            name="Repartidor de prueba",
            phone="3000000000",
            status=CourierStatus.AVAILABLE,
        )
        session.add_all(
            [
                courier,
                Route(
                    courier=courier,
                    status=RouteStatus.COMPLETED,
                    baseline_duration_minutes=20,
                    estimated_duration_minutes=15,
                    baseline_distance_meters=5_000,
                    estimated_distance_meters=4_000,
                ),
                Route(
                    courier=courier,
                    status=RouteStatus.CANCELLED,
                    baseline_duration_minutes=12,
                    estimated_duration_minutes=8,
                    baseline_distance_meters=2_000,
                    estimated_distance_meters=1_000,
                ),
            ]
        )
        await session.commit()

    response = await client.get(
        "/api/analitica",
        headers={"Cookie": _dispatcher_cookie()},
    )

    assert response.status_code == 200
    assert response.json() == {
        "metrics": {
            "routeCount": 1,
            "completedRouteCount": 1,
            "estimatedDurationMinutes": 15.0,
            "estimatedDistanceMeters": 4_000.0,
            "timeSavedMinutes": 5.0,
            "distanceSavedMeters": 1_000.0,
            "routesWithBaseline": 1,
        }
    }


@pytest.mark.asyncio
async def test_analytics_rejects_malformed_courier_id(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/analitica?courierId=not-a-uuid",
        headers={"Cookie": _dispatcher_cookie()},
    )
    assert response.status_code == 400
