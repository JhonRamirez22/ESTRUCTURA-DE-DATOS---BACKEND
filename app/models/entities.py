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


class LocationEvent(Base):
    __tablename__ = "location_events"
    __table_args__ = (
        Index("location_events_courier_recorded_at_idx", "courier_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    courier_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("couriers.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )

    courier: Mapped[Courier] = relationship(back_populates="location_events")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("notifications_recipient_created_at_idx", "recipient_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[NotificationKind] = mapped_column(
        _enum_type(NotificationKind, "notification_kind"),
        nullable=False,
    )
    recipient_id: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CustomerNotificationOutbox(Base):
    """Avisos al cliente con clave idempotente y reintentos fuera de la transacción."""

    __tablename__ = "customer_notification_outbox"
    __table_args__ = (
        UniqueConstraint("event_key", name="customer_notification_outbox_event_key_key"),
        Index(
            "customer_notification_outbox_status_available_idx",
            "status",
            "available_at",
        ),
        Index("customer_notification_outbox_delivery_point_idx", "delivery_point_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_point_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("delivery_points.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    event_key: Mapped[str] = mapped_column(String(220), nullable=False)
    event_type: Mapped[CustomerNotificationEvent] = mapped_column(
        _enum_type(CustomerNotificationEvent, "customer_notification_event"), nullable=False
    )
    channel: Mapped[CustomerNotificationChannel] = mapped_column(
        _enum_type(CustomerNotificationChannel, "customer_notification_channel"),
        nullable=False,
        default=CustomerNotificationChannel.EMAIL,
        server_default=CustomerNotificationChannel.EMAIL.value,
    )
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    recipient_phone: Mapped[str | None] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(180), nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CustomerNotificationStatus] = mapped_column(
        _enum_type(CustomerNotificationStatus, "customer_notification_status"),
        nullable=False,
        default=CustomerNotificationStatus.PENDING,
        server_default=CustomerNotificationStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class CustomerChatRateLimitWindow(Base):
    """Ventana compartida por réplicas; nunca almacena la guía en claro."""

    __tablename__ = "customer_chat_rate_limit_windows"
    __table_args__ = (
        Index(
            "customer_chat_rate_limit_windows_started_at_idx",
            "window_started_at",
        ),
    )

    guide_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)


class TrafficObservation(Base):
    """Medición de TomTom por tramo y hora; sirve para detectar congestión repetida."""

    __tablename__ = "traffic_observations"
    __table_args__ = (
        Index("traffic_observations_observed_hour_idx", "observed_at", "hour_of_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("routes.id", ondelete="SET NULL", onupdate="CASCADE")
    )
    from_stop: Mapped[str] = mapped_column(String(32), nullable=False)
    to_stop: Mapped[str] = mapped_column(String(32), nullable=False)
    multiplier: Mapped[float | None] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    road_closure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    point_lng: Mapped[float] = mapped_column(Float, nullable=False)
    point_lat: Mapped[float] = mapped_column(Float, nullable=False)
    hour_of_day: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
