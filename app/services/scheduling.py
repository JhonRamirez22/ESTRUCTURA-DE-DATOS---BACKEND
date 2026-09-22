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
