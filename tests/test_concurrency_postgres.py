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
    monkeypatch.setattr(order_requests, "_CLIENT_LIMITER", big)
    monkeypatch.setattr(order_requests, "_SERVICE_LIMITER", big)
    async with session_factory() as session:
        await session.execute(
            delete(DeliveryPoint).where(DeliveryPoint.order_id.like("SOL-%"))
        )
        await session.commit()
    try:
        yield session_factory
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(DeliveryPoint).where(DeliveryPoint.order_id.like("SOL-%"))
            )
            await session.commit()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_solicitudes_simultaneas_nunca_superan_el_tope_de_la_bandeja(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """H: contar y luego insertar sin bloqueo dejaba pasar de 25 a 26+ con peticiones paralelas."""
    cap = 5
    monkeypatch.setattr(order_requests, "MAX_WAITING_REQUESTS", cap)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Origin": "http://test"},
    ) as client:
        responses = await asyncio.gather(*[
            client.post(
                "/api/v1/solicitudes",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
                json={"address": f"Calle 18 # 22-{index}", "lat": 1.2136, "lng": -77.2811},
            )
            for index in range(40)
        ])
    assert all(response.status_code in {201, 503} for response in responses)
    async with factory() as session:
        waiting = await session.scalar(
            select(func.count()).select_from(DeliveryPoint)
            .where(DeliveryPoint.status == DeliveryPointStatus.REQUESTED)
        )
    assert waiting is not None and waiting <= cap, f"{waiting} solicitudes en espera, tope {cap}"
