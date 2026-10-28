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


