from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import CustomerChatRateLimitWindow
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter


def test_sliding_window_expires_requests_and_isolates_guides() -> None:
    now = 100.0
    limiter = CustomerChatRateLimiter(
        max_requests=2,
        window_seconds=60,
        clock=lambda: now,
    )

    assert limiter.retry_after("guide-a") is None
    assert limiter.retry_after("guide-a") is None
    assert limiter.retry_after("guide-a") == 60
    assert limiter.retry_after("guide-b") is None

    now += 31
    assert limiter.retry_after("guide-a") == 29
    now += 29
    assert limiter.retry_after("guide-a") is None


def test_cleanup_keeps_the_request_that_triggers_expiration_cleanup() -> None:
    now = 0.0
    limiter = CustomerChatRateLimiter(
        max_requests=1,
        window_seconds=1,
        clock=lambda: now,
    )
    assert limiter.retry_after("guide-a") is None
    for index in range(126):
        assert limiter.retry_after(f"guide-{index}") is None

    now = 1.0
    assert limiter.retry_after("guide-a") is None
    assert limiter.retry_after("guide-a") == 1


@pytest.mark.parametrize(
    ("max_requests", "window_seconds"),
    [(0, 60), (1, 0), (1, -1)],
)
def test_rate_limiter_rejects_invalid_configuration(
    max_requests: int,
    window_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        CustomerChatRateLimiter(max_requests=max_requests, window_seconds=window_seconds)


@pytest.mark.asyncio
async def test_shared_limiter_survives_instances_and_expires_hashed_guide_window() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first_process = CustomerChatRateLimiter(max_requests=2, window_seconds=60)
    second_process = CustomerChatRateLimiter(max_requests=2, window_seconds=60)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    guide = "customer-private-guide"

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            assert await first_process.retry_after_shared(
                session, guide, hmac_secret="auth-secret", now=now
            ) is None
        async with session_factory() as session:
            assert await second_process.retry_after_shared(
                session, guide, hmac_secret="auth-secret", now=now + timedelta(seconds=1)
            ) is None
        async with session_factory() as session:
            assert await second_process.retry_after_shared(
                session, guide, hmac_secret="auth-secret", now=now + timedelta(seconds=2)
            ) == 58

        async with session_factory() as session:
            stored = await session.scalar(select(CustomerChatRateLimitWindow))
            assert stored is not None
            assert stored.guide_hash != guide
            assert len(stored.guide_hash) == 64
            assert stored.request_count == 3

        async with session_factory() as session:
            assert await first_process.retry_after_shared(
                session, guide, hmac_secret="auth-secret", now=now + timedelta(seconds=61)
            ) is None

        async with session_factory() as session:
            reset = await session.scalar(select(CustomerChatRateLimitWindow))
            assert reset is not None
            assert reset.request_count == 1
            assert reset.window_started_at == now.replace(tzinfo=None) + timedelta(seconds=61)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_limiter_requires_a_keyed_guide_fingerprint() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    limiter = CustomerChatRateLimiter()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as session:
            with pytest.raises(ValueError, match="AUTH_SECRET"):
                await limiter.retry_after_shared(session, "guide", hmac_secret="")
    finally:
        await engine.dispose()
