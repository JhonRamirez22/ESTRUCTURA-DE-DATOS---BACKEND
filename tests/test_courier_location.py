from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.location.courier_location import COURIER_LOCATION_MAX_AGE, is_courier_location_fresh


def test_location_freshness_accepts_boundary_and_rejects_missing_old_or_future_fixes() -> None:
    now = datetime(2026, 9, 29, 15, 0)

    assert is_courier_location_fresh(now - COURIER_LOCATION_MAX_AGE, now)
    assert not is_courier_location_fresh(None, now)
    assert not is_courier_location_fresh(now - COURIER_LOCATION_MAX_AGE - timedelta(seconds=1), now)
    assert not is_courier_location_fresh(now + timedelta(seconds=1), now)


def test_location_freshness_normalizes_aware_datetimes_to_utc() -> None:
    now = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
    assert is_courier_location_fresh(now - timedelta(seconds=30), now)
