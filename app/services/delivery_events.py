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

    def __init__(self, session: AsyncSession, clock: Callable[[], datetime] = utc_now) -> None:
        self._session = session
        self._clock = clock

    def record(
        self,
        point: DeliveryPoint,
        event_type: DeliveryEventType,
        actor: EventActor = SYSTEM_ACTOR,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        route_id: UUID | None = None,
        courier_id: UUID | None = None,
        detail: str | None = None,
        keep_link: bool = True,
    ) -> DeliveryEvent:
        event = DeliveryEvent(
            delivery_point_id=point.id if keep_link else None,
            order_ref=point.order_id,
            event_type=event_type.value,
            from_status=from_status,
            to_status=to_status,
            route_id=route_id if route_id is not None else point.route_id,
            courier_id=courier_id,
            actor_role=actor.role,
            actor_account_id=actor.account_id,
            detail=detail[:200] if detail else None,
            occurred_at=self._clock(),
        )
        self._session.add(event)
        return event

    def created(self, point: DeliveryPoint, actor: EventActor) -> None:
        self.record(point, DeliveryEventType.CREATED, actor, to_status=point.status.value)
        if point.scheduled_for is not None:
            self.record(
                point, DeliveryEventType.SCHEDULED, actor, detail=_when(point.scheduled_for)
            )

    def status_changed(
        self,
        point: DeliveryPoint,
        previous: str,
        new: str,
        actor: EventActor,
        *,
        courier_id: UUID | None = None,
    ) -> None:
