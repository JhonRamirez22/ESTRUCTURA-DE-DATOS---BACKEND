"""Mapeo SQLAlchemy de las tablas existentes; nombres y enums no se renumeran."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
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
    credential: Mapped[CourierCredential | None] = relationship(
        back_populates="courier",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class CourierCredential(Base):
    __tablename__ = "courier_credentials"

    courier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("couriers.id", ondelete="CASCADE", onupdate="CASCADE"),
        primary_key=True,
    )
    access_code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )

    courier: Mapped[Courier] = relationship(back_populates="credential")


class DeliveryPoint(Base):
    __tablename__ = "delivery_points"
    __table_args__ = (
        UniqueConstraint("order_id", name="delivery_points_order_id_key"),
        UniqueConstraint("tracking_token", name="delivery_points_tracking_token_key"),
        UniqueConstraint(
            "route_id",
            "sequence_index",
            name="delivery_points_route_sequence_idx",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    time_window: Mapped[object | None] = mapped_column(_JSON_COLUMN_TYPE)
    status: Mapped[DeliveryPointStatus] = mapped_column(
        _enum_type(DeliveryPointStatus, "delivery_point_status"),
        nullable=False,
        default=DeliveryPointStatus.PENDING,
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_email: Mapped[str | None] = mapped_column(String(320))
    email_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    customer_phone: Mapped[str | None] = mapped_column(String(20))
    sms_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    tracking_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("routes.id", ondelete="SET NULL", onupdate="CASCADE"),
    )
    sequence_index: Mapped[int | None] = mapped_column(Integer)

    route: Mapped[Route | None] = relationship(back_populates="delivery_points")


class Route(Base):
    __tablename__ = "routes"
    __table_args__ = (Index("routes_courier_id_idx", "courier_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    courier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("couriers.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    status: Mapped[RouteStatus] = mapped_column(
        _enum_type(RouteStatus, "route_status"),
        nullable=False,
        default=RouteStatus.PLANNED,
    )
    estimated_duration_minutes: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_distance_meters: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_duration_minutes: Mapped[float | None] = mapped_column(Float)
    baseline_distance_meters: Mapped[float | None] = mapped_column(Float)
    geometry: Mapped[object | None] = mapped_column(_JSON_COLUMN_TYPE)
    geometry_provider: Mapped[RouteGeometryProvider | None] = mapped_column(
        _enum_type(RouteGeometryProvider, "route_geometry_provider")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    optimized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True,
        default=func.now(),
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )

    courier: Mapped[Courier] = relationship(back_populates="routes")
    delivery_points: Mapped[list[DeliveryPoint]] = relationship(
        back_populates="route",
        passive_deletes=True,
    )
    revisions: Mapped[list[RouteRevision]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RouteRevision(Base):
    """Snapshot de una ruta antes de recalcularla; permite restauración LIFO durable."""

    __tablename__ = "route_revisions"
    __table_args__ = (
        UniqueConstraint("route_id", "revision_number", name="route_revisions_route_number_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("routes.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(_JSON_COLUMN_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )

    route: Mapped[Route] = relationship(back_populates="revisions")


