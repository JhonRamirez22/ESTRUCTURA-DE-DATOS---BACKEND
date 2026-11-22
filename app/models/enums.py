"""Enums persistidos con los mismos valores nativos de Prisma/PostgreSQL."""

from enum import StrEnum


class CourierStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ON_ROUTE = "ON_ROUTE"
    OFFLINE = "OFFLINE"


class DeliveryPointStatus(StrEnum):
    # Solicitud de un cliente: no entra a la operación hasta que despacho la acepta.
    REQUESTED = "REQUESTED"
    PENDING = "PENDING"
    EN_ROUTE = "EN_ROUTE"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class RouteStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RouteGeometryProvider(StrEnum):
    OSRM = "OSRM"
    ORS = "ORS"


class NotificationKind(StrEnum):
    DELIVERY_STATUS_CHANGED = "DELIVERY_STATUS_CHANGED"
    ROUTE_CREATED = "ROUTE_CREATED"
    ROUTE_RECALCULATED = "ROUTE_RECALCULATED"
    ROUTE_RECALCULATION_FAILED = "ROUTE_RECALCULATION_FAILED"


class CustomerNotificationEvent(StrEnum):
    ORDER_ASSIGNED = "ORDER_ASSIGNED"
    COURIER_NEAR = "COURIER_NEAR"


class CustomerNotificationChannel(StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"


class CustomerNotificationStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SENT = "SENT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class DeliveryEventType(StrEnum):
    """What happened to a shipment. Stored as text so new kinds never need an enum migration."""

    REQUESTED = "REQUESTED"
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SCHEDULED = "SCHEDULED"
    RESCHEDULED = "RESCHEDULED"
    ASSIGNED = "ASSIGNED"
    STARTED = "STARTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    RELEASED = "RELEASED"
    DELETED = "DELETED"
