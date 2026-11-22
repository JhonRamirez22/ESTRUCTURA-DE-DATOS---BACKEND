"""Scheduled delivery: when a shipment was asked for and whether it is due, soon or late."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.services.access_sessions import utc_now

BOGOTA_OFFSET = timedelta(hours=-5)
MAX_DAYS_AHEAD = 60
PAST_TOLERANCE = timedelta(minutes=5)
DEFAULT_LEAD_MINUTES = 60


class ScheduleState(StrEnum):
    """Where a scheduled shipment stands; shown as a badge and used to sort the queue."""

    SCHEDULED = "scheduled"  # still far away: waits in the «Programados» queue
    DUE_SOON = "due-soon"  # inside the lead window: ready to dispatch
    OVERDUE = "overdue"  # the time has passed and it was not dispatched


class DeliverySchedule:
    """Parses, validates and classifies the instant a customer asked for."""

    def __init__(
        self,
        *,
        lead_minutes: int = DEFAULT_LEAD_MINUTES,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._lead = timedelta(minutes=lead_minutes)
        self._clock = clock

    def parse(self, value: datetime | None) -> datetime | None:
        """A naive UTC instant. A value without zone is read as Bogotá local time."""
        if value is None:
            return None
        if value.tzinfo is None:
            instant = value - BOGOTA_OFFSET
        else:
            instant = value.astimezone(UTC).replace(tzinfo=None)
        now = self._clock()
        if instant < now - PAST_TOLERANCE:
            raise ValueError("scheduledFor must not be in the past.")
        if instant > now + timedelta(days=MAX_DAYS_AHEAD):
            raise ValueError(f"scheduledFor can be at most {MAX_DAYS_AHEAD} days ahead.")
        return instant

    def state(self, scheduled_for: datetime | None) -> ScheduleState | None:
        if scheduled_for is None:
            return None
        now = self._clock()
        if scheduled_for < now:
            return ScheduleState.OVERDUE
        if scheduled_for <= now + self._lead:
            return ScheduleState.DUE_SOON
        return ScheduleState.SCHEDULED

    @staticmethod
    def sort_key(state: ScheduleState | None, priority: int, scheduled_for: datetime | None):  # type: ignore[no-untyped-def]
        """Queue order: overdue first, then due soon, then priority, then the rest by time."""
        rank = {ScheduleState.OVERDUE: 0, ScheduleState.DUE_SOON: 1}.get(state, 2)  # type: ignore[arg-type]
        return (rank, -priority, scheduled_for or datetime.max)
