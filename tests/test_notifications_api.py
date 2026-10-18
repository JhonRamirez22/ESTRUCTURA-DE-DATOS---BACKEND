from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_token
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    NotificationKind,
)
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = "f8fbc95f-643e-4fb3-9b82-8c42e0707311"


class _RunningWorkerTask:
    def done(self) -> bool:
        return False


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.sqlite'}")
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


def _cookie(role: str, courier_id: str | None = None) -> str:
    token = create_session_token(
        role, int(datetime.now().timestamp()) + SESSION_MAX_AGE_SECONDS, _SESSION_SECRET, courier_id
    )
    return f"{SESSION_COOKIE_NAME}={token}"


@pytest.mark.asyncio
async def test_notifications_reject_anonymous_and_prevent_recipient_enumeration(
    client: httpx.AsyncClient,
) -> None:
    anonymous = await client.get("/api/notificaciones")
    courier = await client.get(
        "/api/notificaciones?recipientId=dispatcher",
        headers={"Cookie": _cookie("courier", _COURIER_ID)},
    )

    assert anonymous.status_code == 401
    assert courier.status_code == 403
    assert courier.json() == {"error": "No puedes consultar avisos de otro usuario."}


@pytest.mark.asyncio
async def test_dispatcher_lists_only_dispatcher_notifications(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                Notification(
                    kind=NotificationKind.ROUTE_CREATED,
                    recipient_id="dispatcher",
                    title="Ruta asignada",
                    message="La ruta está lista.",
                ),
                Notification(
                    kind=NotificationKind.ROUTE_CREATED,
                    recipient_id=_COURIER_ID,
                    title="Privada de repartidor",
