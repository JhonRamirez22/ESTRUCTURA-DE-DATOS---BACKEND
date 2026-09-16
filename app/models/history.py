"""Append-only shipment history: one row per change in a shipment's life."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeliveryEvent(Base):
    """A timestamped fact about a shipment.

    Rows are never updated or deleted. ``order_ref`` keeps the order reference so the history
    survives the deletion of the shipment itself (the foreign key is then set to NULL).
    """

    __tablename__ = "delivery_events"
    __table_args__ = (
        Index("delivery_events_point_time_idx", "delivery_point_id", "occurred_at"),
        Index("delivery_events_time_idx", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    delivery_point_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("delivery_points.id", ondelete="SET NULL", onupdate="CASCADE")
    )
    order_ref: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    route_id: Mapped[UUID | None] = mapped_column(Uuid)
    courier_id: Mapped[UUID | None] = mapped_column(Uuid)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_account_id: Mapped[UUID | None] = mapped_column(Uuid)
    detail: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
