"""Vigencia de posiciones GPS de repartidores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

COURIER_LOCATION_MAX_AGE = timedelta(minutes=2)


def is_courier_location_fresh(
    last_location_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    if last_location_at is None:
        return False
    current_time = datetime.now(UTC).replace(tzinfo=None) if now is None else _as_naive_utc(now)
    location_time = _as_naive_utc(last_location_at)
    age = current_time - location_time
    return timedelta(0) <= age <= COURIER_LOCATION_MAX_AGE


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
