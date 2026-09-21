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
