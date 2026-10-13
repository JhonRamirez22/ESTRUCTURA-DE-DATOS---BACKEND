"""Planificación y persistencia atómica de rutas optimizadas."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
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
from app.core.optimization.or_opt import or_opt
from app.core.optimization.order_verifier import verify_candidate_order
from app.core.optimization.refine import RefineResult, refine_route
from app.core.optimization.time_windows import (
    Window,
    WindowEvaluation,
    plan_with_windows,
    window_from_json,
)
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
from app.core.structures.lru_cache import LruCache
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
    TrafficObservation,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.route_repository import RouteRepository
from app.services.customer_notifications import enqueue_order_assigned_notification
from app.services.delivery_events import SYSTEM_ACTOR, DeliveryEventRecorder, EventActor
from app.services.route_order_advisor import MAX_AI_ORDER_STOPS, RouteOrderAdvisor
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
    ai_order: dict[str, object] = field(default_factory=dict)
    # Cómo se llegó al orden final: escalera de mejoras, tráfico medido e IA verificada.
    optimization: dict[str, object] = field(default_factory=dict)


# Último resumen de optimización por ruta, en memoria del proceso (LRU con lista doble).
# Al reiniciar se pierde: la ruta conserva sus métricas base y optimizada en la base de datos.
ROUTE_OPTIMIZATIONS: LruCache[UUID, dict[str, object]] = LruCache(128)


def get_route_optimization(route_id: UUID) -> dict[str, object] | None:
    return ROUTE_OPTIMIZATIONS.get(route_id)


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


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """Lo que el plan necesita de la base de datos; cargarlo aparte permite calcular en paralelo."""

    courier: Courier | None
    points: list[DeliveryPoint]


async def load_plan_inputs(
    session: AsyncSession, courier_id: UUID, delivery_point_ids: Sequence[UUID]
) -> PlanInputs:
    repository = RouteRepository(session)
    courier = await repository.get_courier(courier_id)
    points = await repository.get_delivery_points(delivery_point_ids)
    return PlanInputs(courier, points)


async def build_optimized_route_plan(
    session: AsyncSession,
    courier_id: UUID,
    delivery_point_ids: Sequence[UUID],
    *,
    inputs: PlanInputs | None = None,
    matrix_provider: MatrixProvider,
    route_provider: RouteProvider,
    traffic_ai_advisor: RouteTrafficAdvisor | None = None,
    traffic_provider: TrafficProvider | None = None,
    order_advisor: RouteOrderAdvisor | None = None,
