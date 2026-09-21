"""Records the life of every shipment: who did what to it and when.

The recorder only adds rows to the session; the caller commits, so an event is stored in the
same transaction as the change it describes (no event for a change that rolled back).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeliveryEvent, DeliveryEventType, DeliveryPoint
from app.services.access_sessions import utc_now


@dataclass(frozen=True, slots=True)
class EventActor:
    """Who caused an event: a signed-in role, a customer, or the system itself."""

    role: str
    account_id: UUID | None = None

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any] | None) -> EventActor:
        if not claims:
            return SYSTEM_ACTOR
        raw = claims.get("accountId")
        try:
            account = UUID(str(raw)) if raw else None
        except ValueError:
            account = None
        return cls(str(claims.get("role", "system")), account)


SYSTEM_ACTOR = EventActor("system")
CUSTOMER_ACTOR = EventActor("customer")

_STATUS_EVENT = {
    "DELIVERED": DeliveryEventType.DELIVERED,
    "FAILED": DeliveryEventType.FAILED,
}


class DeliveryEventRecorder:
    """Adds :class:`DeliveryEvent` rows for the shipments a request touches."""
