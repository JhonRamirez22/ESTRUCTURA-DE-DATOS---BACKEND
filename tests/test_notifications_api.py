from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
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
