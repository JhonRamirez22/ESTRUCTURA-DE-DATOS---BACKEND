"""Mapeo SQLAlchemy de las tablas existentes; nombres y enums no se renumeran."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    CourierStatus,
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationStatus,
    DeliveryPointStatus,
    NotificationKind,
    RouteGeometryProvider,
    RouteStatus,
)

_JSON_COLUMN_TYPE = JSON().with_variant(JSONB(), "postgresql")


def _enum_type(enum_class: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_class,
        name=name,
        native_enum=True,
        values_callable=lambda values: [value.value for value in values],
    )


class Courier(Base):
    __tablename__ = "couriers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CourierStatus] = mapped_column(
        _enum_type(CourierStatus, "courier_status"),
        nullable=False,
        default=CourierStatus.OFFLINE,
    )
    current_lat: Mapped[float | None] = mapped_column(Float)
    current_lng: Mapped[float | None] = mapped_column(Float)
    # Prisma's PostgreSQL DateTime columns are TIMESTAMP WITHOUT TIME ZONE.
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    routes: Mapped[list[Route]] = relationship(back_populates="courier", passive_deletes=True)
    location_events: Mapped[list[LocationEvent]] = relationship(
        back_populates="courier",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
