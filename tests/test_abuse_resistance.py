"""Resistencia a abuso: el bloqueo de un atacante no debe bloquear a los demás.

Hipótesis confirmadas con la API real (ver auditoría de seguridad):
- H1: el límite de login se aplicaba por correo, así que cualquiera que conociera el correo del
  despachador podía mantenerlo fuera con pocas peticiones por minuto.
- H2: el límite de solicitudes públicas tomaba el primer valor de X-Forwarded-For, que el cliente
  controla; con valores distintos agotaba el cupo global y bloqueaba a los clientes reales.
- H3: el backend aceptaba peticiones directas aunque debía hablar solo con Vercel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import auth, order_requests
from app.core.passwords import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Account
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

SECRET = "abuse-resistance-secret-with-more-than-32-bytes"
PASSWORD = "Una contraseña privada 2026"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'abuse.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(
            Account(email="despacho@example.com", password_hash=hash_password(PASSWORD),
                    role="dispatcher")
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    # Contadores limpios: los limitadores viven en memoria del proceso.
    monkeypatch.setattr(
        auth, "_LOGIN_LIMITER", CustomerChatRateLimiter(max_requests=10, window_seconds=60)
    )
    per_client = CustomerChatRateLimiter(max_requests=3, window_seconds=600)
    service = CustomerChatRateLimiter(max_requests=20, window_seconds=600)
    monkeypatch.setattr(order_requests, "_CLIENT_LIMITER", per_client)
    monkeypatch.setattr(order_requests, "_SERVICE_LIMITER", service)
    monkeypatch.setattr(
        auth, "_LOGIN_EMAIL_LIMITER", CustomerChatRateLimiter(max_requests=60, window_seconds=600)
    )

