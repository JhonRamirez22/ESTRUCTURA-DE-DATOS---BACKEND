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
        kind = _STATUS_EVENT.get(new)
        if kind is None:
            return  # other status moves are recorded as assignment, start or release
        self.record(point, kind, actor, from_status=previous, to_status=new, courier_id=courier_id)

    def rescheduled(
        self, point: DeliveryPoint, before: datetime | None, after: datetime | None,
        actor: EventActor,
    ) -> None:
        kind = DeliveryEventType.SCHEDULED if before is None else DeliveryEventType.RESCHEDULED
        detail = f"{_when(before)} → {_when(after)}"
        self.record(point, kind, actor, detail=detail)

    def assigned(
        self, points: Iterable[DeliveryPoint], route_id: UUID, courier_id: UUID,
        actor: EventActor,
    ) -> None:
        for point in points:
            self.record(
                point, DeliveryEventType.ASSIGNED, actor, route_id=route_id, courier_id=courier_id
            )

    def started(self, points: Iterable[DeliveryPoint], actor: EventActor) -> None:
        for point in points:
            self.record(
                point, DeliveryEventType.STARTED, actor,
                from_status="PENDING", to_status="EN_ROUTE",
            )

    def deleted(self, point: DeliveryPoint, actor: EventActor) -> None:
        """The shipment row is gone, so the event keeps only its order reference."""
        self.record(
            point, DeliveryEventType.DELETED, actor,
            from_status=point.status.value, keep_link=False,
        )

    def rejected(self, point: DeliveryPoint, actor: EventActor) -> None:
        self.record(
            point, DeliveryEventType.REJECTED, actor,
            from_status="REQUESTED", keep_link=False,
        )

    def released(self, points: Iterable[DeliveryPoint], actor: EventActor, detail: str) -> None:
        for point in points:
            self.record(point, DeliveryEventType.RELEASED, actor, detail=detail)


def _when(moment: datetime | None) -> str:
    return "—" if moment is None else moment.strftime("%Y-%m-%d %H:%M UTC")
