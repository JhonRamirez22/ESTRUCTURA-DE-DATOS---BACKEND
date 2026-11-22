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
    position: tuple[float, float] | None,
    stops: list[tuple[str, float, float]],
    status: RouteStatus = RouteStatus.IN_PROGRESS,
) -> UUID:
    courier = Courier(
        id=uuid4(), name=name, phone=f"+57300{uuid4().int % 10_000_000:07d}",
        status=CourierStatus.ON_ROUTE,
        current_lat=position[0] if position else None,
        current_lng=position[1] if position else None,
        last_location_at=datetime.now(UTC).replace(tzinfo=None),
    )
    route = Route(
        id=uuid4(), courier_id=courier.id, status=status,
        estimated_duration_minutes=30, estimated_distance_meters=5000,
    )
    async with factory() as session:
        session.add_all([courier, route])
        await session.flush()
        for index, (label, lat, lng) in enumerate(stops):
            session.add(DeliveryPoint(
                id=uuid4(), address=label, lat=lat, lng=lng, order_id=f"{name}-{label}",
                tracking_token=f"t-{uuid4()}", route_id=route.id, sequence_index=index,
                status=DeliveryPointStatus.EN_ROUTE if index == 0 else DeliveryPointStatus.PENDING,
            ))
        await session.commit()
    return route.id


_URL = "/api/v1/optimizaciones/en-vivo"


async def _live(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get(_URL, headers=_cookie("dispatcher"))


A_STOPS = [("a1", 1.2005, -77.2990), ("a2", 1.2010, -77.2980), ("x", 1.2005, -77.2505)]
B_STOPS = [("b1", 1.2000, -77.2490), ("b2", 1.2010, -77.2480)]


@pytest.mark.asyncio
async def test_only_dispatch_can_read_the_live_proposal(setup: Any) -> None:
    client, _, _ = setup
    assert (await client.get(_URL)).status_code == 401
    courier = await client.get(_URL, headers=_cookie("courier", uuid4()))
    assert courier.status_code in {401, 403}


@pytest.mark.asyncio
async def test_no_active_routes_returns_an_empty_proposal_without_calling_providers(
    setup: Any,
) -> None:
    client, matrix, routes = setup
    response = await _live(client)
    body = response.json()
    assert response.status_code == 200 and body["routes"] == [] and body["changed"] is False
    assert body["tickSeconds"] == 10.0 and matrix.calls == 0 and routes.calls == 0


@pytest.mark.asyncio
async def test_proposes_moving_a_stop_to_the_nearer_courier_with_geometry_and_scores(
    setup: Any, factory: async_sessionmaker[AsyncSession]
) -> None:
    client, matrix, routes = setup
    a = await _seed_route(factory, "Ana", (1.2000, -77.3000), A_STOPS)
    b = await _seed_route(factory, "Beto", (1.2000, -77.2500), B_STOPS)
    response = await _live(client)
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["changed"] is True and body["matrixSource"] == "osrm"
    assert len(body["moves"]) == 1
    move = body["moves"][0]
    assert move["fromRouteId"] == str(a) and move["toRouteId"] == str(b)
    by_route = {item["routeId"]: item for item in body["routes"]}
    assert move["stopId"] in by_route[str(b)]["movedIn"]
    assert move["stopId"] in by_route[str(a)]["movedOut"]
    assert by_route[str(b)]["geometry"]["type"] == "LineString"
    assert body["scoreAfter"]["totalMinutes"] < body["scoreBefore"]["totalMinutes"]
    assert body["savedMinutes"] > 0
    # The first stop of a route in progress is locked: it is still first in the proposal.
    assert by_route[str(a)]["proposedOrder"][0] == by_route[str(a)]["currentOrder"][0]
    assert matrix.calls == 1 and routes.calls == 2


@pytest.mark.asyncio
async def test_unchanged_state_is_served_from_the_ten_second_cache(
    setup: Any, factory: async_sessionmaker[AsyncSession]
) -> None:
    client, matrix, _ = setup
    await _seed_route(factory, "Ana", (1.2000, -77.3000), A_STOPS)
    await _seed_route(factory, "Beto", (1.2000, -77.2500), B_STOPS)
    first = (await _live(client)).json()
    second = (await _live(client)).json()
    assert first["cached"] is False and second["cached"] is True
    assert matrix.calls == 1 and second["routes"] == first["routes"]


@pytest.mark.asyncio
async def test_courier_without_position_is_skipped_and_reported(
    setup: Any, factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _, _ = setup
    lost = await _seed_route(factory, "Sin GPS", None, B_STOPS)
    await _seed_route(factory, "Ana", (1.2000, -77.3000), A_STOPS)
    body = (await _live(client)).json()
    assert body["skippedRoutes"] == [{"routeId": str(lost), "reason": "no-courier-position"}]
    assert len(body["routes"]) == 1


@pytest.mark.asyncio
async def test_provider_failure_is_a_clean_502_and_flag_off_disables(
    setup: Any, factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, matrix, _ = setup
    await _seed_route(factory, "Ana", (1.2000, -77.3000), A_STOPS)
    matrix.fail = True
    down = await _live(client)
    assert down.status_code == 502 and "osrm" not in down.text.lower()
    monkeypatch.setenv("LIVE_OPTIMIZATION_ENABLED", "false")
    get_settings.cache_clear()
    off = await _live(client)
    assert off.json() == {"enabled": False, "routes": []}


@pytest.mark.asyncio
async def test_polling_is_rate_limited(setup: Any) -> None:
    client, _, _ = setup
    codes = [(await _live(client)).status_code for _ in range(23)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


@pytest.mark.asyncio
async def test_payload_names_the_next_stop_of_each_route_and_the_closest_one(
    setup: Any, factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _, _ = setup
    a = await _seed_route(factory, "Ana", (1.2000, -77.3000), A_STOPS)
    b = await _seed_route(factory, "Beto", (1.2000, -77.2500), B_STOPS)
    body = (await _live(client)).json()
    nexts = {item["routeId"]: item for item in body["nextStops"]}
    assert set(nexts) == {str(a), str(b)}
    assert sum(1 for item in nexts.values() if item["focus"]) == 1
    focus = next(item for item in nexts.values() if item["focus"])
    assert body["focusRouteId"] == focus["routeId"]
    assert all(focus["etaMinutes"] <= item["etaMinutes"] for item in nexts.values())
    kinds = {item["kind"] for item in body["explanations"]}
    assert {"next", "move"} <= kinds
    assert body["explanationSource"] == "deterministic"  # no AI configured in tests
    move = next(item for item in body["explanations"] if item["kind"] == "move")
    assert "{stop}" in move["text"] and "Ana" not in move["text"] and "Beto" not in move["text"]
