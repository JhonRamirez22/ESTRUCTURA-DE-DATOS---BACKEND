from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import Account, Courier, DeliveryPoint
from app.models.security import AccessSession
from app.services.access_sessions import (
    STAFF_SESSION_IDLE_LIMIT,
    STAFF_SESSION_LIFETIME,
    create_access_session,
    csrf_matches,
    csrf_token,
    resolve_access_session,
    revoke_access_session,
    revoke_account_sessions,
    token_digest,
)

NOW = datetime(2026, 10, 18, 12)
SECRET = "unit-test-secret-not-a-production-credential"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as value:
            yield value
    finally:
        await engine.dispose()


async def account(session: AsyncSession) -> Account:
    value = Account(email=f"{uuid4()}@example.test", password_hash="test", role="dispatcher")
    session.add(value)
    await session.flush()
    return value


@pytest.mark.asyncio
async def test_token_is_random_hashed_and_resolves_current_account(session: AsyncSession) -> None:
    owner = await account(session)
    first = await create_access_session(session, account_id=owner.id, now=NOW)
    second = await create_access_session(session, account_id=owner.id, now=NOW)
    assert first != second
    assert len(first) == 43
    stored = await session.get(AccessSession, token_digest(first))
    assert stored is not None
    assert stored.token_digest != first
    assert stored.expires_at == NOW + STAFF_SESSION_LIFETIME
    identity = await resolve_access_session(session, first, now=NOW)
    assert identity is not None
