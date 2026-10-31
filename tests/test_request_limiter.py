from __future__ import annotations

import pytest

from app.services.request_limiter import SlidingWindowLimiter


def test_limiter_blocks_after_the_limit_and_recovers_when_the_window_passes() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    assert [limiter.retry_after("cuenta") for _ in range(3)] == [None, None, None]
    assert limiter.retry_after("cuenta") == 60
    now[0] = 30
    assert limiter.retry_after("cuenta") == 30
    assert limiter.retry_after("otra") is None
    now[0] = 61
    assert limiter.retry_after("cuenta") is None


def test_limiter_rejects_invalid_configuration_and_bounds_memory() -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(0, 60)
    limiter = SlidingWindowLimiter(1, 1, clock=lambda: 0.0)
    for index in range(10_050):
        limiter.retry_after(f"cuenta-{index}")
    assert len(limiter._hits) <= 10_050


def test_account_limit_returns_429_with_retry_after_and_separates_reads_from_writes() -> None:
    from fastapi import HTTPException

    from app.api import auth
    from app.settings import Settings

    auth._ACCOUNT_LIMITERS.clear()
    settings = Settings(_env_file=None, account_write_limit_per_minute=5)  # type: ignore[call-arg]
    for _ in range(5):
        auth._enforce_account_limit(settings, "cuenta-limite", "POST")
    with pytest.raises(HTTPException) as blocked:
        auth._enforce_account_limit(settings, "cuenta-limite", "PATCH")
    assert blocked.value.status_code == 429
    assert "Retry-After" in (blocked.value.headers or {})
    # Las lecturas tienen su propio cupo.
    auth._enforce_account_limit(settings, "cuenta-limite", "GET")
    auth._ACCOUNT_LIMITERS.clear()
