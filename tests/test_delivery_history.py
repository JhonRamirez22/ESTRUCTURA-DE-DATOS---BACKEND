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


def _kinds(events: list[DeliveryEvent]) -> list[str]:
    return [e.event_type for e in events]


# ---- history -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_shipment_leaves_an_event_for_each_step_of_its_life(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    courier_id = await _courier(factory)
    point = await _create(client, "H-1")
    assert point["createdAt"] and point["deliveredAt"] is None
    applied = await client.post(
        "/api/asignaciones/aplicar", headers=_cookie("dispatcher"),
        json={"assignments": [{"courierId": str(courier_id), "deliveryPointIds": [point["id"]]}]},
    )
    assert applied.status_code == 201, applied.text
    route_id = applied.json()["routes"][0]["route"]["id"]
    started = await client.patch(
        f"/api/rutas/{route_id}", headers=_cookie("dispatcher"), json={"status": "IN_PROGRESS"}
    )
    assert started.status_code == 200, started.text
    done = await client.patch(
        f"/api/pedidos/{point['id']}", headers=_cookie("courier", courier_id),
        json={"status": "DELIVERED"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["deliveryPoint"]["deliveredAt"] is not None

    events = await _events(factory, "H-1")
    assert set(_kinds(events)) == {"CREATED", "ASSIGNED", "STARTED", "DELIVERED"}
    by_kind = {e.event_type: e for e in events}
    assert by_kind["CREATED"].actor_role == "dispatcher"
    assert by_kind["ASSIGNED"].route_id == UUID(route_id)
    assert by_kind["ASSIGNED"].courier_id == courier_id
    assert (by_kind["STARTED"].from_status, by_kind["STARTED"].to_status) == ("PENDING", "EN_ROUTE")
    assert by_kind["DELIVERED"].actor_role == "courier"
    assert by_kind["DELIVERED"].courier_id == courier_id
    times = [e.occurred_at for e in events]
    assert times == sorted(times)


@pytest.mark.asyncio
async def test_cancelling_a_route_releases_its_shipments_and_failures_are_recorded(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    courier_id = await _courier(factory)
    point = await _create(client, "H-2")
    applied = await client.post(
        "/api/asignaciones/aplicar", headers=_cookie("dispatcher"),
        json={"assignments": [{"courierId": str(courier_id), "deliveryPointIds": [point["id"]]}]},
    )
    route_id = applied.json()["routes"][0]["route"]["id"]
    cancelled = await client.patch(
        f"/api/rutas/{route_id}", headers=_cookie("dispatcher"), json={"status": "CANCELLED"}
    )
    assert cancelled.status_code == 200, cancelled.text
    released = [e for e in await _events(factory, "H-2") if e.event_type == "RELEASED"]
    assert len(released) == 1 and released[0].detail == "route cancelled"

    failed = await _create(client, "H-3")
    patched = await client.patch(
        f"/api/pedidos/{failed['id']}", headers=_cookie("dispatcher"), json={"status": "FAILED"}
    )
    assert patched.status_code == 200
    only = await _events(factory, "H-3")
    assert "FAILED" in _kinds(only)
    assert next(e for e in only if e.event_type == "FAILED").from_status == "PENDING"


@pytest.mark.asyncio
async def test_deleting_a_shipment_keeps_its_history_without_the_link(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    point = await _create(client, "H-4")
    gone = await client.delete(f"/api/pedidos/{point['id']}", headers=_cookie("dispatcher"))
    assert gone.status_code == 204
    events = await _events(factory, "H-4")
    assert set(_kinds(events)) == {"CREATED", "DELETED"}
    deleted = next(e for e in events if e.event_type == "DELETED")
    assert deleted.delivery_point_id is None and deleted.from_status == "PENDING"
    async with factory() as session:
        assert await session.get(DeliveryPoint, UUID(point["id"])) is None


@pytest.mark.asyncio
async def test_customer_requests_record_request_acceptance_and_rejection(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with factory() as session:
        accepted = DeliveryPoint(
            id=uuid4(), address="Calle 1", lat=1.2136, lng=-77.2811, status="REQUESTED",
            order_id="SOL-A", tracking_token=f"t-{uuid4()}",
        )
        rejected = DeliveryPoint(
            id=uuid4(), address="Calle 2", lat=1.2136, lng=-77.2811, status="REQUESTED",
            order_id="SOL-B", tracking_token=f"t-{uuid4()}",
        )
        session.add_all([accepted, rejected])
        await session.commit()
    head = _cookie("dispatcher")
    ok = await client.post(f"/api/solicitudes/{accepted.id}/aceptar", headers=head)
    no = await client.post(f"/api/solicitudes/{rejected.id}/rechazar", headers=head)
    assert ok.status_code == 200 and no.status_code == 200
    accepted_events = await _events(factory, "SOL-A")
    assert _kinds(accepted_events) == ["ACCEPTED"]
    moved = (accepted_events[0].from_status, accepted_events[0].to_status)
    assert moved == ("REQUESTED", "PENDING")
    rejected_events = await _events(factory, "SOL-B")
    assert _kinds(rejected_events) == ["REJECTED"]
    assert rejected_events[0].delivery_point_id is None


# ---- scheduling --------------------------------------------------------------------------


def _bogota(moment_utc: datetime) -> str:
    return (moment_utc + BOGOTA_OFFSET).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.mark.asyncio
async def test_scheduled_delivery_is_stored_in_utc_labelled_and_recorded(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    soon = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30)
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=3)
    near = await _create(client, "S-1", scheduledFor=_bogota(soon))  # a naive value is Bogotá time
    far = await _create(client, "S-2", scheduledFor=later.replace(tzinfo=UTC).isoformat())
    plain = await _create(client, "S-3")
    assert near["scheduleState"] == "due-soon" and far["scheduleState"] == "scheduled"
    assert plain["scheduleState"] is None and plain["scheduledFor"] is None
    stored = datetime.fromisoformat(near["scheduledFor"].replace("Z", "+00:00"))
    assert abs((stored.replace(tzinfo=None) - soon).total_seconds()) < 2
    assert set(_kinds(await _events(factory, "S-1"))) == {"CREATED", "SCHEDULED"}


@pytest.mark.asyncio
async def test_rejects_past_and_too_distant_schedules(client: httpx.AsyncClient) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    for value in (now - timedelta(hours=2), now + timedelta(days=MAX_DAYS_AHEAD + 2)):
        response = await client.post(
            "/api/pedidos", headers=_cookie("dispatcher"),
            json={**_ORDER, "orderId": f"X-{uuid4().hex[:6]}",
                  "scheduledFor": value.replace(tzinfo=UTC).isoformat()},
        )
        assert response.status_code == 400 and "scheduledFor" in response.text


@pytest.mark.asyncio
async def test_rescheduling_and_clearing_are_recorded_with_before_and_after(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    point = await _create(client, "S-4")
    later = datetime.now(UTC) + timedelta(days=2)
    first = await client.patch(
        f"/api/pedidos/{point['id']}", headers=_cookie("dispatcher"),
        json={"scheduledFor": later.isoformat()},
    )
    assert first.status_code == 200
    assert first.json()["deliveryPoint"]["scheduleState"] == "scheduled"
    cleared = await client.patch(
        f"/api/pedidos/{point['id']}", headers=_cookie("dispatcher"), json={"scheduledFor": None}
    )
    assert cleared.status_code == 200 and cleared.json()["deliveryPoint"]["scheduledFor"] is None
    events = [e for e in await _events(factory, "S-4") if "SCHEDULED" in e.event_type]
    assert [e.event_type for e in events] == ["SCHEDULED", "RESCHEDULED"]
    assert events[1].detail is not None and events[1].detail.endswith("→ —")


# ---- DeliverySchedule --------------------------------------------------------------------


def test_schedule_states_and_queue_order() -> None:
    now = datetime(2026, 11, 23, 12, 0)
    schedule = DeliverySchedule(lead_minutes=60, clock=lambda: now)
    assert schedule.state(None) is None
    assert schedule.state(now - timedelta(minutes=1)) is ScheduleState.OVERDUE
    assert schedule.state(now + timedelta(minutes=59)) is ScheduleState.DUE_SOON
    assert schedule.state(now + timedelta(minutes=61)) is ScheduleState.SCHEDULED
    overdue = DeliverySchedule.sort_key(ScheduleState.OVERDUE, 1, now - timedelta(hours=1))
    soon = DeliverySchedule.sort_key(ScheduleState.DUE_SOON, 3, now + timedelta(minutes=10))
    urgent = DeliverySchedule.sort_key(None, 3, None)
    normal = DeliverySchedule.sort_key(None, 1, None)
    assert sorted([normal, urgent, soon, overdue]) == [overdue, soon, urgent, normal]


def test_schedule_parse_boundaries() -> None:
    now = datetime(2026, 11, 23, 12, 0)
    schedule = DeliverySchedule(clock=lambda: now)
    assert schedule.parse(None) is None
    assert schedule.parse(datetime(2026, 11, 23, 9, 0)) == datetime(2026, 11, 23, 14, 0)
    assert schedule.parse(now - timedelta(minutes=4)) is not None  # inside the tolerance
    with pytest.raises(ValueError):
        schedule.parse(now.replace(tzinfo=UTC) - timedelta(minutes=10))
    with pytest.raises(ValueError):
        schedule.parse(now.replace(tzinfo=UTC) + timedelta(days=MAX_DAYS_AHEAD + 1))


# ---- history API and customer milestones ---------------------------------------------------


async def _lifecycle(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession], ref: str
) -> tuple[dict[str, Any], UUID]:
    courier_id = await _courier(factory)
    point = await _create(client, ref)
    applied = await client.post(
        "/api/asignaciones/aplicar", headers=_cookie("dispatcher"),
        json={"assignments": [{"courierId": str(courier_id), "deliveryPointIds": [point["id"]]}]},
    )
    route_id = applied.json()["routes"][0]["route"]["id"]
    await client.patch(
        f"/api/rutas/{route_id}", headers=_cookie("dispatcher"), json={"status": "IN_PROGRESS"}
    )
    await client.patch(
        f"/api/pedidos/{point['id']}", headers=_cookie("courier", courier_id),
        json={"status": "DELIVERED"},
