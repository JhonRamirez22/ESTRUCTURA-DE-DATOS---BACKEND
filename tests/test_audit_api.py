"""Auditoría para el despacho: permisos, contenido del PDF, enmascarado y registro de descargas."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from time import time
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteStatus,
)
from app.models.security import SecurityAuditEvent
from app.settings import get_settings

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = UUID("f8fbc95f-643e-4fb3-9b82-8c42e0707311")
_TODAY = (datetime.now(UTC) + timedelta(hours=-5)).date().isoformat()  # día local de Bogotá


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.sqlite'}")

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
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()


def _cookie(role: str, courier_id: UUID | None = None) -> dict[str, str]:
    token = create_session_token(
        role,  # type: ignore[arg-type]
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SECRET,
        str(courier_id) if courier_id else None,
    )
    return {"Cookie": f"{session_cookie_name(role)}={token}"}  # type: ignore[arg-type]


def _pdf_text(pdf: bytes) -> str:
    """Texto visible del PDF: cadenas de los operadores Tj, decodificadas de cp1252."""
    out: list[str] = []
    for match in re.finditer(rb"\((.*?)(?<!\\)\) Tj", pdf):
        raw = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]), match.group(1))
        out.append(raw.replace(b"\\(", b"(").replace(b"\\)", b")").decode("cp1252"))
    return "\n".join(out)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> UUID:
    now = datetime.now(UTC).replace(tzinfo=None)
    point_id = uuid4()
    async with factory() as session:
        session.add(Courier(
            id=_COURIER_ID, name="Ana Repartidora", phone="3001112233",
            status=CourierStatus.AVAILABLE, current_lat=1.2136, current_lng=-77.2811,
            last_location_at=now,
        ))
        route = Route(
            id=uuid4(), courier_id=_COURIER_ID, status=RouteStatus.COMPLETED,
            estimated_duration_minutes=30, estimated_distance_meters=5000,
            baseline_duration_minutes=40, started_at=now - timedelta(hours=1),
            completed_at=now - timedelta(minutes=20),
        )
        session.add(route)
        await session.flush()
        session.add(DeliveryPoint(
            id=point_id, address="Carrera 25 # 4 sur 65", lat=1.214, lng=-77.28,
            status=DeliveryPointStatus.DELIVERED, order_id="RP-1001", tracking_token="t-1",
            customer_email="cliente.privado@example.com", email_notifications_enabled=True,
            customer_phone="3109998877", route_id=route.id, sequence_index=0, priority=3,
        ))
        for step in range(3):
            session.add(LocationEvent(
                courier_id=_COURIER_ID, lat=1.2136 + step * 0.001, lng=-77.2811,
                recorded_at=now - timedelta(minutes=30 - step),
            ))
        await session.flush()
        session.add(CustomerNotificationOutbox(
            delivery_point_id=point_id, event_key="ek-1",
            event_type=CustomerNotificationEvent.ORDER_ASSIGNED,
            channel=CustomerNotificationChannel.EMAIL,
            recipient_email="cliente.privado@example.com", subject="Tu pedido", text_body="cuerpo",
            status=CustomerNotificationStatus.SENT, attempt_count=1, sent_at=now,
        ))
        await session.commit()
    return point_id


def _query(kind: str, subject: UUID) -> dict[str, str]:
    return {"tipo": kind, "id": str(subject), "desde": _TODAY, "hasta": _TODAY}


@pytest.mark.asyncio
async def test_only_dispatch_can_use_audit(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/auditoria/sujetos?tipo=courier")).status_code == 401
    courier = _cookie("courier", _COURIER_ID)
    assert (await client.get("/api/v1/auditoria/sujetos?tipo=courier", headers=courier)
            ).status_code in {401, 403}
    denied = await client.get("/api/v1/auditoria/informe", params=_query("courier", uuid4()),
                              headers=courier)
    assert denied.status_code in {401, 403}


@pytest.mark.asyncio
async def test_subject_search_filters_and_masks_contacts(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(factory)
