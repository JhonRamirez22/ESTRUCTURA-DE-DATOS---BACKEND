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
