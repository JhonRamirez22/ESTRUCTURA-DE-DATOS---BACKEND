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
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ai.assignment_traffic import (
    AssignmentTrafficAdjustment,
    AssignmentTrafficAdvice,
    AssignmentTrafficRequest,
)
from app.core.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
)
from app.core.routing.contracts import Coordinate, RouteResult
from app.db.base import Base
from app.db.session import get_session, get_session_factory
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    Route,
    RouteStatus,
)
from app.services.route_planner import get_routing_providers
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


class FakeMatrixProvider:
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        size = len(coordinates)
        durations = [
            [0.0 if source == target else float(3 + abs(source - target)) for target in range(size)]
            for source in range(size)
        ]
        distances = [
            [0.0 if source == target else float(300 + 100 * abs(source - target))
             for target in range(size)]
            for source in range(size)
        ]
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
            "distance_meters": 1_600,
            "duration_minutes": 11.5,
        }


class FakeTrafficAdvisor:
    def __init__(self, *, fail_route_advice: bool = False) -> None:
        self.assignment_request: AssignmentTrafficRequest | None = None
        self.fail_route_advice = fail_route_advice

    async def get_assignment_advice(
        self,
        request: AssignmentTrafficRequest,
    ) -> AssignmentTrafficAdvice:
        self.assignment_request = request
        candidate = request.candidates[0]
        return AssignmentTrafficAdvice(
            (
                AssignmentTrafficAdjustment(
                    candidate.courier_id,
                    candidate.order_id,
                    1.4,
                ),
            ),
            model="test-model",
        )

    async def get_route_advice(self, _request: object) -> Any:
        if self.fail_route_advice:
            raise RuntimeError("AI unavailable")
        return {
            "adjustments": (),
            "model": "test-model",
        }


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assignments.sqlite'}")

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
    monkeypatch.setenv("NOTIFICATION_SMTP_HOST", "")
    monkeypatch.setenv("NOTIFICATION_SMTP_USERNAME", "")
    monkeypatch.setenv("NOTIFICATION_SMTP_PASSWORD", "")
    monkeypatch.setenv("NOTIFICATION_FROM_EMAIL", "")
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("NOTIFICATION_SMS_TWILIO_FROM_PHONE", "")
    get_settings.cache_clear()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_routing_providers] = lambda: (
        FakeMatrixProvider(),
        FakeRouteProvider(),
    )
    app.dependency_overrides[get_route_traffic_advisor] = lambda: None
    app.dependency_overrides[get_traffic_service] = lambda: None
    try:
