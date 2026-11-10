from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.passwords import hash_password
from app.db.session import get_session
from app.main import app
from app.models import AccessSession, Account, CustomerChatRateLimitWindow, SecurityAuditEvent
from app.services.access_sessions import token_digest
from app.settings import get_settings


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="Requiere PostgreSQL local"
)
@pytest.mark.asyncio
async def test_real_postgres_login_cookie_csrf_and_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    assert settings.sqlalchemy_database_url.host in {"127.0.0.1", "localhost"}
    secret = "postgres-security-test-secret-at-least-32-bytes"
    monkeypatch.setenv("AUTH_SECRET", secret)
    monkeypatch.setenv("NODE_ENV", "test")
    get_settings.cache_clear()
    engine = create_async_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid4()
    email = f"{owner_id}@example.test"
    password = "Postgres test private password"

    async def local_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = local_session
    try:
        async with factory() as session:
            session.add(Account(
                id=owner_id, email=email, password_hash=hash_password(password), role="dispatcher",
            ))
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            login = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": password},
            )
            assert login.status_code == 200
            token = client.cookies.get("rutas-pasto-dispatch-session")
            assert token is not None
            async with factory() as session:
                record = await session.get(AccessSession, token_digest(token))
                assert record is not None and record.account_id == owner_id
                assert record.token_digest != token
            assert (await client.get("/api/v1/auth/session")).json() == {"authenticated": True}
            denied = await client.post("/api/v1/auth/logout")
            assert denied.status_code == 403
            logout = await client.post(
                "/api/v1/auth/logout", headers={"X-CSRF-Token": login.headers["X-CSRF-Token"]},
            )
            assert logout.status_code == 200
            replay = await client.get(
                "/api/v1/auth/session", headers={"Cookie": f"rutas-pasto-dispatch-session={token}"},
            )
            assert replay.json() == {"authenticated": False}
        async with factory() as session:
            record = await session.get(AccessSession, token_digest(token))
            assert record is not None and record.revoked_at is not None
            actions = await session.scalars(
                select(SecurityAuditEvent.action).where(SecurityAuditEvent.account_id == owner_id)
            )
            assert set(actions) == {"login", "logout"}
    finally:
        app.dependency_overrides.pop(get_session, None)
        async with factory() as session:
            await session.execute(delete(SecurityAuditEvent).where(
                SecurityAuditEvent.account_id == owner_id,
            ))
            await session.execute(delete(AccessSession).where(AccessSession.account_id == owner_id))
            await session.execute(delete(Account).where(Account.id == owner_id))
            key = hmac.new(secret.encode(), f"login:{email}".encode(), hashlib.sha256).hexdigest()
            await session.execute(delete(CustomerChatRateLimitWindow).where(
                CustomerChatRateLimitWindow.guide_hash == key,
            ))
            await session.commit()
        await engine.dispose()
        get_settings.cache_clear()
