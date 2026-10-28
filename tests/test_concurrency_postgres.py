"""Carreras reales contra PostgreSQL: lo que una prueba con SQLite o secuencial no ve."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import order_requests
from app.db.session import get_session
from app.main import app
from app.models import DeliveryPoint, DeliveryPointStatus
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Activa RUN_POSTGRES_INTEGRATION=1 para usar la base PostgreSQL configurada.",
)

_SECRET = "concurrency-secret-with-more-than-32-bytes!!"


@pytest_asyncio.fixture
async def factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    monkeypatch.setenv("AUTH_SECRET", _SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    get_settings.cache_clear()
    engine = create_async_engine(get_settings().sqlalchemy_database_url, pool_size=30)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    # Los límites por cliente no son lo que se prueba aquí.
    big = CustomerChatRateLimiter(max_requests=10_000, window_seconds=600)
