"""Planificación y persistencia atómica de rutas optimizadas."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.route_traffic import (
    RouteTrafficAiRequest,
    apply_route_traffic_adjustments,
    build_route_traffic_candidates,
    filter_route_traffic_adjustments,
    select_route_traffic_candidates,
)
from app.core.location.courier_location import is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.two_opt import two_opt
from app.core.routing.cached_matrix_provider import (
    CachedMatrixProvider,
    MatrixClient,
    MatrixProviderResult,
    MatrixSource,
)
from app.core.routing.cached_route_provider import CachedRouteProvider, RouteClient
from app.core.routing.contracts import (
    Coordinate,
    GeoJsonLineString,
    GeometryProvider,
    RouteResult,
)
from app.core.routing.ors_client import OrsClient
from app.core.routing.osrm_client import OsrmClient
from app.core.structures.stack import Stack
from app.core.traffic.route_sampling import sample_route_leg_points
from app.core.traffic.tomtom import TrafficData
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationChannel,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    NotificationKind,
    Route,
    RouteGeometryProvider,
    RouteRevision,
    RouteStatus,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.route_repository import RouteRepository
from app.services.customer_notifications import enqueue_order_assigned_notification
from app.services.route_traffic_advisor import RouteTrafficAdvisor
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
MAX_ROUTE_STOPS = 50
MAX_ROUTE_REVISIONS = 20
MAX_ROUTE_TRAFFIC_AI_CANDIDATES = 32
_ACTIVE_ROUTE_STATUSES = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)
_ACTIVE_POINT_STATUSES = (DeliveryPointStatus.PENDING, DeliveryPointStatus.EN_ROUTE)
_TERMINAL_POINT_STATUSES = (DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED)
MAX_TOMTOM_ROUTE_SAMPLES = 8
MIN_TOMTOM_CONFIDENCE = 0.3


class MatrixProvider(Protocol):
    async def get_duration_matrix(
        self, coordinates: Sequence[Coordinate]
    ) -> MatrixProviderResult: ...


class RouteProvider(Protocol):
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult: ...


class TrafficProvider(Protocol):
    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData: ...


class ClosableRoutingClient(Protocol):
    async def aclose(self) -> None: ...


class RoutePlanningError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class RouteConflictError(RoutePlanningError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 409)


@dataclass(frozen=True, slots=True)
class OptimizedRoutePlan:
    courier_id: UUID
    planned_courier_location: Coordinate
    ordered_delivery_point_ids: tuple[UUID, ...]
    planned_delivery_point_coordinates: tuple[tuple[UUID, Coordinate], ...]
    estimated_duration_minutes: float
    estimated_distance_meters: float
    baseline_duration_minutes: float | None
    baseline_distance_meters: float | None
    geometry: GeoJsonLineString
    geometry_provider: RouteGeometryProvider
    matrix_source: str
    warning: str | None
    traffic_ai: dict[str, object]
    live_traffic: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouteOperationResult:
    route: Route
    ordered_delivery_point_ids: tuple[UUID, ...]
    matrix_source: str
    warning: str | None
    traffic_ai: dict[str, object]
    customer_notifications_queued: int = 0
    customer_notifications_queued_by_channel: tuple[
        tuple[CustomerNotificationChannel, int], ...
    ] = ()
    live_traffic: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouteUndoResult:
    route: Route
    can_undo: bool
    released_delivery_point_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _RouteRevisionSnapshot:
    estimated_duration_minutes: float
    estimated_distance_meters: float
    baseline_duration_minutes: float | None
    baseline_distance_meters: float | None
    geometry: object | None
    geometry_provider: RouteGeometryProvider | None
    active_stops: tuple[tuple[UUID, int], ...]


_routing_clients: tuple[
    ClosableRoutingClient,
    CachedMatrixProvider,
    CachedRouteProvider,
] | None = None


def get_routing_providers() -> tuple[CachedMatrixProvider, CachedRouteProvider]:
    """Comparte el proveedor vial elegido y sus cachés durante la vida del proceso."""
    global _routing_clients
    if _routing_clients is None:
        settings = get_settings()
        if settings.routing_provider == "ors":
            api_key = (
                settings.ors_api_key.get_secret_value().strip()
                if settings.ors_api_key is not None
                else ""
            )
            if not api_key:
                raise ValueError("ROUTING_PROVIDER=ors requiere ORS_API_KEY.")
            client: object = OrsClient(
                api_key=api_key,
                base_url=settings.ors_api_url,
            )
            matrix_source: MatrixSource = "ors"
            geometry_provider: GeometryProvider = "ORS"
        else:
            client = OsrmClient(base_url=settings.osrm_api_url)
            matrix_source = "osrm"
            geometry_provider = "OSRM"
        _routing_clients = (
            cast(ClosableRoutingClient, client),
            CachedMatrixProvider(cast(MatrixClient, client), source=matrix_source),
            CachedRouteProvider(cast(RouteClient, client), provider=geometry_provider),
        )
    return _routing_clients[1], _routing_clients[2]


async def close_routing_clients() -> None:
    global _routing_clients
    if _routing_clients is not None:
        await _routing_clients[0].aclose()
        _routing_clients = None


async def build_optimized_route_plan(
    session: AsyncSession,
    courier_id: UUID,
    delivery_point_ids: Sequence[UUID],
    *,
    matrix_provider: MatrixProvider,
    route_provider: RouteProvider,
    traffic_ai_advisor: RouteTrafficAdvisor | None = None,
    traffic_provider: TrafficProvider | None = None,
    allow_on_route_courier: bool = False,
    existing_route_id: UUID | None = None,
    now: datetime | None = None,
) -> OptimizedRoutePlan:
    if not delivery_point_ids:
        raise RoutePlanningError("deliveryPointIds debe ser un array no vacío.", 400)
    if len(delivery_point_ids) > MAX_ROUTE_STOPS:
        raise RoutePlanningError(f"Una ruta no puede superar {MAX_ROUTE_STOPS} paradas.", 400)
    if len(set(delivery_point_ids)) != len(delivery_point_ids):
        raise RoutePlanningError("deliveryPointIds no puede contener duplicados.", 400)

    current_time = _as_naive_utc(now or datetime.now(UTC))
    repository = RouteRepository(session)
    courier = await repository.get_courier(courier_id)
    if courier is None:
        raise RoutePlanningError("El courier solicitado no existe.", 404)
    can_recalculate = allow_on_route_courier and courier.status == CourierStatus.ON_ROUTE
    if courier.status != CourierStatus.AVAILABLE and not can_recalculate:
        raise RouteConflictError("El courier no está AVAILABLE.")
    if courier.current_lat is None or courier.current_lng is None:
        raise RouteConflictError("El courier no tiene una posición actual registrada.")
    if not is_courier_location_fresh(courier.last_location_at, current_time):
        raise RouteConflictError(
            "La ubicación GPS del repartidor está vencida. Debe compartir una posición reciente."
        )
    if not is_within_pasto_service_area(courier.current_lat, courier.current_lng):
        raise RouteConflictError("La última ubicación del repartidor está fuera de Pasto.")

    points = await repository.get_delivery_points(delivery_point_ids)
    points_by_id = {point.id: point for point in points}
    missing_id = next(
        (point_id for point_id in delivery_point_ids if point_id not in points_by_id),
        None,
    )
    if missing_id is not None:
        raise RoutePlanningError(f"El delivery point {missing_id} no existe.", 404)

    for point_id in delivery_point_ids:
        point = points_by_id[point_id]
        active = point.status == DeliveryPointStatus.PENDING or (
            point.status == DeliveryPointStatus.EN_ROUTE and point.route_id == existing_route_id
        )
        if not active or (point.route_id is not None and point.route_id != existing_route_id):
            raise RouteConflictError(
                f"El delivery point {point_id} no está PENDING o ya pertenece a otra ruta."
            )
        if not is_within_pasto_service_area(point.lat, point.lng):
            raise RouteConflictError(
                f"El delivery point {point_id} está fuera de la zona de servicio de Pasto."
            )

    coordinates: list[Coordinate] = [(courier.current_lng, courier.current_lat)]
    coordinates.extend(
        (points_by_id[point_id].lng, points_by_id[point_id].lat) for point_id in delivery_point_ids
    )

    matrix_result = await matrix_provider.get_duration_matrix(coordinates)
    baseline_matrix = matrix_result["durations_minutes"]
    matrix_source = matrix_result.get("source", "osrm")
    has_road_times = matrix_source in {"osrm", "ors"}
    provider_name = matrix_source.upper() if has_road_times else "el proveedor vial"
    _validate_matrix_shape(baseline_matrix, len(coordinates))
    baseline_route = nearest_neighbor(baseline_matrix, 0)
    optimization_matrix = baseline_matrix
    traffic_ai: dict[str, object] = {
        "source": "deterministic",
        "applied": False,
        "warning": (
            "IA de tráfico no configurada o permiso de ubicaciones desactivado; "
            f"se optimizó con tiempos viales de {provider_name}, no con distancia en línea recta."
        ),
