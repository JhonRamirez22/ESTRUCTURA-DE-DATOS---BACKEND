"""Shipment history and scheduled delivery: every change leaves an event with its actor."""

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

from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.core.location.proximity import haversine_m
from app.core.routing.contracts import Coordinate
from app.db.base import Base
from app.db.session import get_session, get_session_factory
from app.main import app
from app.models import Courier, CourierStatus, DeliveryEvent, DeliveryPoint
from app.services.route_order_advisor import get_route_order_advisor
from app.services.route_planner import get_routing_providers
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.scheduling import (
    BOGOTA_OFFSET,
    MAX_DAYS_AHEAD,
    DeliverySchedule,
    ScheduleState,
)
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_ORDER = {"address": "Carrera 25 # 4 sur 65", "lat": 1.2136, "lng": -77.2811}


class _Matrix:
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        rows = [
            [0.0 if a == b else haversine_m(a[1], a[0], b[1], b[0]) / 400.0 for b in coordinates]
            for a in coordinates
        ]
        return {"durations_minutes": rows, "source": "osrm"}


class _Roads:
    async def get_route(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM", "distance_meters": 1000.0, "duration_minutes": 5.0,
        }


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.sqlite'}")

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
async def client(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", _SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    get_settings.cache_clear()

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_routing_providers] = lambda: (_Matrix(), _Roads())
    app.dependency_overrides[get_route_traffic_advisor] = lambda: None
    app.dependency_overrides[get_traffic_service] = lambda: None
    app.dependency_overrides[get_route_order_advisor] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        for dependency in (
            get_session_factory, get_routing_providers, get_route_traffic_advisor,
            get_traffic_service, get_route_order_advisor,
        ):
            app.dependency_overrides.pop(dependency, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: UUID | None = None) -> dict[str, str]:
    token = create_session_token(
        role,  # type: ignore[arg-type]
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SECRET,
        str(courier_id) if courier_id else None,
    )
    return {"Cookie": f"{session_cookie_name(role)}={token}"}  # type: ignore[arg-type]


async def _events(factory: async_sessionmaker[AsyncSession], ref: str) -> list[DeliveryEvent]:
    async with factory() as session:
        rows = await session.scalars(
            select(DeliveryEvent).where(DeliveryEvent.order_ref == ref)
            .order_by(DeliveryEvent.occurred_at.asc(), DeliveryEvent.event_type.asc())
        )
        return list(rows.all())


async def _courier(factory: async_sessionmaker[AsyncSession]) -> UUID:
    courier = Courier(
        id=uuid4(), name="Ana", phone="+573001112233", status=CourierStatus.AVAILABLE,
        current_lat=1.2136, current_lng=-77.2811,
        last_location_at=datetime.now(UTC).replace(tzinfo=None),
    )
    async with factory() as session:
        session.add(courier)
        await session.commit()
    return courier.id


async def _create(client: httpx.AsyncClient, ref: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/pedidos", headers=_cookie("dispatcher"), json={**_ORDER, "orderId": ref, **extra}
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["deliveryPoint"])

