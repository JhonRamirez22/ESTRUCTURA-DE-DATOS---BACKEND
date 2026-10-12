"""Reads the shipment history: a filtered list, one shipment's timeline and customer milestones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Courier,
    DeliveryEvent,
    DeliveryEventType,
    DeliveryPoint,
    DeliveryPointStatus,
    Route,
)

PAGE_SIZE_MAX = 50
# What a customer may see about their own shipment: when, never who or where.
CUSTOMER_EVENTS = (
    DeliveryEventType.CREATED,
    DeliveryEventType.SCHEDULED,
    DeliveryEventType.ASSIGNED,
    DeliveryEventType.STARTED,
    DeliveryEventType.DELIVERED,
    DeliveryEventType.FAILED,
)


@dataclass(frozen=True, slots=True)
class HistoryFilters:
    """Criteria of the history list; every field is optional."""

    begin: datetime | None = None
    end: datetime | None = None
    status: DeliveryPointStatus | None = None
    courier_id: UUID | None = None
    text: str = ""
    scheduled_only: bool = False


@dataclass(frozen=True, slots=True)
class HistoryItem:
    point_id: UUID
    order_ref: str
    address: str
    status: str
    courier_name: str | None
    created_at: datetime
    delivered_at: datetime | None
    scheduled_for: datetime | None
    events: int
    last_event_at: datetime | None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: list[HistoryItem]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    event_type: str
    occurred_at: datetime
    from_status: str | None
    to_status: str | None
    actor_role: str
    courier_name: str | None
    detail: str | None


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class DeliveryHistoryReader:
    """Read-only queries over shipments and their events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self, filters: HistoryFilters, *, page: int = 1, page_size: int = 25
    ) -> HistoryPage:
        page = max(1, page)
        page_size = max(1, min(page_size, PAGE_SIZE_MAX))
        conditions = self._conditions(filters)
        base = (
            select(DeliveryPoint, Courier.name)
            .outerjoin(Route, DeliveryPoint.route_id == Route.id)
            .outerjoin(Courier, Route.courier_id == Courier.id)
            .where(*conditions)
        )
        total = int(
            await self._session.scalar(
                select(func.count())
                .select_from(DeliveryPoint)
                .outerjoin(Route, DeliveryPoint.route_id == Route.id)
                .where(*conditions)
            )
            or 0
        )
        rows = (
            await self._session.execute(
                base.order_by(DeliveryPoint.created_at.desc(), DeliveryPoint.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).all()
        ids = [point.id for point, _ in rows]
        counts: dict[UUID, tuple[int, datetime | None]] = {}
        if ids:
            stats = await self._session.execute(
                select(
                    DeliveryEvent.delivery_point_id,
                    func.count(),
                    func.max(DeliveryEvent.occurred_at),
                )
                .where(DeliveryEvent.delivery_point_id.in_(ids))
                .group_by(DeliveryEvent.delivery_point_id)
            )
            counts = {pid: (int(n), last) for pid, n, last in stats.all() if pid is not None}
        items = [
            HistoryItem(
                point.id, point.order_id, point.address, point.status.value, name,
                point.created_at, point.delivered_at, point.scheduled_for,
                *counts.get(point.id, (0, None)),
            )
            for point, name in rows
        ]
        return HistoryPage(items, total, page, page_size)

    @staticmethod
    def _conditions(filters: HistoryFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.begin is not None:
            conditions.append(DeliveryPoint.created_at >= filters.begin)
        if filters.end is not None:
            conditions.append(DeliveryPoint.created_at < filters.end)
        if filters.status is not None:
            conditions.append(DeliveryPoint.status == filters.status)
        if filters.courier_id is not None:
            conditions.append(Route.courier_id == filters.courier_id)
        if filters.scheduled_only:
            conditions.append(DeliveryPoint.scheduled_for.is_not(None))
        text = filters.text.strip()
        if text:
            conditions.append(
                or_(
                    DeliveryPoint.order_id.ilike(_like(text), escape="\\"),
                    DeliveryPoint.address.ilike(_like(text), escape="\\"),
                )
            )
        return conditions

    async def timeline(self, point_id: UUID) -> list[TimelineEntry] | None:
        """Events of one shipment oldest first; ``None`` if the shipment does not exist."""
        if await self._session.get(DeliveryPoint, point_id) is None:
            return None
        events = (
            await self._session.scalars(
                select(DeliveryEvent)
                .where(DeliveryEvent.delivery_point_id == point_id)
                .order_by(DeliveryEvent.occurred_at.asc(), DeliveryEvent.id.asc())
            )
        ).all()
        courier_ids = {e.courier_id for e in events if e.courier_id is not None}
        names: dict[UUID, str] = {}
        if courier_ids:
            rows = await self._session.execute(
                select(Courier.id, Courier.name).where(Courier.id.in_(courier_ids))
            )
            names = {cid: name for cid, name in rows.all()}
        return [
            TimelineEntry(
                e.event_type, e.occurred_at, e.from_status, e.to_status, e.actor_role,
                names.get(e.courier_id) if e.courier_id else None, e.detail,
            )
            for e in events
        ]

    async def customer_milestones(self, point_id: UUID) -> list[tuple[str, datetime]]:
        """Only the moments a customer may see, without actors, couriers or places."""
        rows = await self._session.execute(
            select(DeliveryEvent.event_type, DeliveryEvent.occurred_at)
            .where(
                and_(
                    DeliveryEvent.delivery_point_id == point_id,
                    DeliveryEvent.event_type.in_([e.value for e in CUSTOMER_EVENTS]),
                )
            )
            .order_by(DeliveryEvent.occurred_at.asc(), DeliveryEvent.id.asc())
        )
        return [(kind, when) for kind, when in rows.all()]
