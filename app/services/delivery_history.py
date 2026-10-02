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
