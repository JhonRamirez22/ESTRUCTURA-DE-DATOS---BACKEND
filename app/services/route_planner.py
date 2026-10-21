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
    if inputs is None:
        inputs = await load_plan_inputs(session, courier_id, delivery_point_ids)
    courier = inputs.courier
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

    points = inputs.points
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
    initial_route = two_opt(baseline_matrix, baseline_route)
    if not math.isfinite(_route_cost(baseline_matrix, initial_route)):
        raise RouteConflictError("No existe un camino conectado que cubra todos los pedidos.")
    ordered_coordinates = [coordinates[index] for index in initial_route]
    route_result = await route_provider.get_route(ordered_coordinates)
    geometry_provider = route_result["geometry_provider"]
    if geometry_provider not in {"OSRM", "ORS"}:
        raise RoutePlanningError(
            "El proveedor no verificó una geometría vial; no se guardará una línea recta.", 502
        )
    live_traffic: dict[str, object] = {
        "source": "osrm-baseline",
        "configured": traffic_provider is not None,
        "applied": False,
        "routeSegments": len(initial_route) - 1,
        "sampledSegments": 0,
        "adjustedSegments": 0,
        "closedSegments": 0,
        "coverageRatio": 0.0,
        "warning": "TomTom no disponible; se conservan los tiempos viales de base.",
    }
    if traffic_provider is not None and has_road_times:
        optimization_matrix, live_traffic = await _apply_live_traffic_to_route(
            traffic_provider, baseline_matrix, initial_route, ordered_coordinates,
            route_result["geometry"], baseline_matrix=baseline_matrix,
        )
    observations = cast(list[dict[str, object]], live_traffic.get("observations", []))
    measured_pairs = {
        (item["fromStopId"], item["toStopId"]) for item in observations
    }
    traffic_ai: dict[str, object] = {
        "source": "deterministic",
        "applied": False,
        "warning": (
            "IA de tráfico no configurada o permiso de ubicaciones desactivado; "
            f"se optimizó con tiempos viales de {provider_name}, no con distancia en línea recta."
        ),
    }

    if traffic_ai_advisor is None and traffic_provider is not None and has_road_times:
        traffic_ai = {
            "source": "tomtom-live",
            "applied": False,
            "warning": "Se usa TomTom en los tramos muestreados y OSRM para el resto.",
        }
    elif traffic_ai_advisor is not None and not has_road_times:
        traffic_ai["warning"] = (
            "La IA no se consultó porque la matriz es un fallback, "
            "no tiempos de calles verificados."
        )
    elif traffic_ai_advisor is not None:
        try:
            stop_ids = [f"stop-{index}" for index in range(len(coordinates))]
            road_distances = matrix_result.get("distances_meters")
            all_candidates = build_route_traffic_candidates(
                stop_ids,
                coordinates,
                baseline_matrix,
                distance_matrix=(
                    road_distances
                    if road_distances is not None
                    else [[None for _ in coordinates] for _ in coordinates]
                ),
                distance_source=matrix_source,
            )
            baseline_stop_ids = [stop_ids[index] for index in baseline_route]
            candidates = select_route_traffic_candidates(
                all_candidates,
                baseline_stop_ids,
                MAX_ROUTE_TRAFFIC_AI_CANDIDATES,
            )
            if not candidates:
                raise ValueError("No hay pares con conexión vial para asesoría de IA.")
            advice = await traffic_ai_advisor.get_route_advice(
                RouteTrafficAiRequest(
                    tuple(candidates), current_time.replace(tzinfo=UTC),
                    traffic_observations=tuple(observations),
                )
            )
            supported, ignored_count = filter_route_traffic_adjustments(
                candidates, advice.adjustments
            )
            # Una sugerencia del modelo nunca sustituye una medición ni reabre un cierre.
            supported = [
                item for item in supported
                if (item.from_stop_id, item.to_stop_id) not in measured_pairs
            ]
            optimization_matrix = apply_route_traffic_adjustments(
                optimization_matrix, stop_ids, supported
            )
            warnings: list[str] = []
            if supported:
                warnings.append(
                    "La IA aportó estimaciones para tramos sin medición; "
                    "TomTom conserva prioridad en los tramos observados."
                )
            elif ignored_count:
                warnings.append(
                    "La IA devolvió tramos ajenos a la ruta; se usaron tiempos base "
                    "del proveedor vial."
                )
            else:
                warnings.append(
                    "La IA respondió sin ajustes; se usaron tiempos base del proveedor vial."
                )
            if len(candidates) < len(all_candidates):
                warnings.append(
                    f"La IA evaluó {len(candidates)} pares priorizados; "
                    "los demás conservaron tiempos base."
                )
            traffic_ai = {
                "source": "external-ai",
                "applied": bool(supported),
                "evaluated": True,
                "model": advice.model,
                "warning": " ".join(warnings),
            }
        except Exception as error:
            # El payload puede incluir coordenadas; solo se registra el tipo del error.
            _LOGGER.warning(
                "Falló el asesor de tráfico; se usan tiempos viales base",
                extra={"error_type": type(error).__name__},
            )
            traffic_ai = {
                "source": "deterministic",
                "applied": False,
                "warning": (
                    "La IA de tráfico no respondió de forma utilizable; se optimizó con "
                    "tiempos viales base."
                ),
            }

    # No descartar una solución previa mejor por reiniciar la heurística con nuevos pesos.
    seed = min(
        (baseline_route, initial_route, nearest_neighbor(optimization_matrix, 0)),
        key=lambda route: _route_cost(optimization_matrix, route),
    )
    two_opt_route = two_opt(optimization_matrix, seed)
    # Or-opt traslada tramos de 1 a 3 paradas que 2-opt no corrige con una inversión.
    optimized_route = or_opt(optimization_matrix, two_opt_route)
    # Comparar ordenaciones con los mismos costos, no tráfico actual contra flujo libre.
    baseline_duration = _route_cost(optimization_matrix, baseline_route)
    optimized_duration = _route_cost(optimization_matrix, optimized_route)
    if not math.isfinite(optimized_duration):
        raise RouteConflictError("No existe un camino conectado que cubra todos los pedidos.")

    or_opt_duration = optimized_duration
    # Óptimo exacto (Held-Karp) en rutas cortas y búsqueda local iterada en largas; es CPU puro,
    # así que no bloquea el bucle de eventos.
    refined = await asyncio.to_thread(refine_route, optimization_matrix, optimized_route)
    optimized_route = refined.route
    optimized_duration = refined.cost
    ai_order = await _verified_ai_order(
        order_advisor, optimization_matrix, optimized_route, has_road_times
    )
    if ai_order["accepted"]:
        optimized_route = cast(list[int], ai_order.pop("route"))
        optimized_duration = _route_cost(optimization_matrix, optimized_route)
    ai_order.pop("route", None)

    window_summary = (
        _apply_time_windows(
            optimization_matrix, optimized_route, delivery_point_ids, points_by_id, current_time
        )
        if get_settings().time_windows_enabled
        else None
    )
    if window_summary is not None:
        optimized_route = cast(list[int], window_summary.pop("route"))
        optimized_duration = _route_cost(optimization_matrix, optimized_route)

    final_coordinates = [coordinates[index] for index in optimized_route]
    if final_coordinates != ordered_coordinates:
        route_result = await route_provider.get_route(final_coordinates)
        geometry_provider = route_result["geometry_provider"]
        if geometry_provider not in {"OSRM", "ORS"}:
            raise RoutePlanningError("El proveedor no verificó una geometría vial.", 502)
    final_measured = sum(
        (f"stop-{start}", f"stop-{end}") in measured_pairs
        for start, end in zip(optimized_route, optimized_route[1:], strict=False)
    )
    live_traffic["usedMeasuredSegments"] = final_measured
    live_traffic["coverageRatio"] = final_measured / max(1, len(optimized_route) - 1)

    ordered_ids = tuple(delivery_point_ids[index - 1] for index in optimized_route[1:])
    baseline_geometry: GeoJsonLineString | None = None
    if baseline_route != optimized_route:
        try:
            # Solo para mostrar la diferencia en el mapa; no participa en la decisión.
            baseline_geometry = (
                await route_provider.get_route([coordinates[index] for index in baseline_route])
            )["geometry"]
        except Exception as error:
            _LOGGER.info(
                "No se obtuvo el trazado del orden base para comparar",
                extra={"error_type": type(error).__name__},
            )
    optimization = _optimization_summary(
        delivery_point_ids=delivery_point_ids,
        matrix=optimization_matrix,
        baseline_route=baseline_route,
        two_opt_route=two_opt_route,
        or_opt_duration=or_opt_duration,
        refined=refined,
        final_route=optimized_route,
        baseline_geometry=baseline_geometry,
        live_traffic=live_traffic,
        traffic_ai=traffic_ai,
        ai_order=ai_order,
        computed_at=current_time,
    )
    if window_summary is not None:
        optimization["timeWindows"] = window_summary
    distances = matrix_result.get("distances_meters")
    baseline_distance = (
        _route_cost(distances, baseline_route)
        if distances is not None and has_road_times
        else math.inf
    )

    return OptimizedRoutePlan(
        courier_id=courier_id,
        planned_courier_location=(courier.current_lng, courier.current_lat),
        ordered_delivery_point_ids=ordered_ids,
        planned_delivery_point_coordinates=tuple(
            (point_id, (points_by_id[point_id].lng, points_by_id[point_id].lat))
            for point_id in delivery_point_ids
        ),
        estimated_duration_minutes=(
            optimized_duration
            if live_traffic["applied"] or traffic_ai["applied"]
            else route_result["duration_minutes"]
        ),
        estimated_distance_meters=route_result["distance_meters"],
        baseline_duration_minutes=(
            baseline_duration if has_road_times and math.isfinite(baseline_duration) else None
        ),
        baseline_distance_meters=baseline_distance if math.isfinite(baseline_distance) else None,
        geometry=route_result["geometry"],
        geometry_provider=RouteGeometryProvider(geometry_provider),
        matrix_source=str(matrix_source),
        warning=matrix_result.get("warning"),
        traffic_ai=traffic_ai,
        live_traffic=live_traffic,
        ai_order=ai_order,
        optimization=optimization,
    )


_BOGOTA_OFFSET = timedelta(hours=-5)


def _apply_time_windows(
    matrix: Sequence[Sequence[float | None]],
    route: list[int],
    delivery_point_ids: Sequence[UUID],
    points_by_id: Mapping[UUID, DeliveryPoint],
    current_time: datetime,
) -> dict[str, object] | None:
    """Reordena para cumplir ventanas y prioridades; ``None`` si ningún pedido las usa."""
    local = current_time + _BOGOTA_OFFSET
    now_minute = local.hour * 60 + local.minute + local.second / 60
    windows: dict[int, Window] = {}
    priority: dict[int, float] = {}
    for index, point_id in enumerate(delivery_point_ids, start=1):
        point = points_by_id[point_id]
        window = window_from_json(point.time_window, now_minute)
        if window is not None:
            windows[index] = window
        if point.priority > 1:
            priority[index] = float(point.priority)
    if not windows:
        return None
    plan = plan_with_windows(matrix, route, windows, priority)
    changed = plan.route != route and plan.after.objective < plan.before.objective - 1e-9
    return {
        "applied": changed,
        "route": plan.route if changed else route,
        "windowedStops": len(windows),
        "lateStopsBefore": plan.before.late_stops,
        "lateStopsAfter": plan.after.late_stops if changed else plan.before.late_stops,
        "lateMinutesBefore": round(_plain_lateness(plan.before, windows, route), 1),
        "lateMinutesAfter": round(
            _plain_lateness(plan.after, windows, plan.route) if changed
            else _plain_lateness(plan.before, windows, route), 1,
        ),
    }


def _plain_lateness(
    evaluation: WindowEvaluation, windows: Mapping[int, Window], route: Sequence[int]
) -> float:
    """Minutos de retraso sin ponderar por prioridad, para mostrarlos al despachador."""
    total = 0.0
    for node, arrival in zip(route, evaluation.arrivals, strict=False):
        window = windows.get(node)
        if window is not None and arrival > window.latest:
            total += arrival - window.latest
    return total


def _optimization_summary(
    *,
    delivery_point_ids: Sequence[UUID],
    matrix: Sequence[Sequence[float | None]],
    baseline_route: Sequence[int],
    two_opt_route: Sequence[int],
    or_opt_duration: float,
    refined: RefineResult,
    final_route: Sequence[int],
    baseline_geometry: GeoJsonLineString | None,
    live_traffic: Mapping[str, object],
    traffic_ai: Mapping[str, object],
    ai_order: Mapping[str, object],
    computed_at: datetime,
) -> dict[str, object]:
    """Explica la ruta con costos de la misma matriz ajustada por tráfico."""

    def minutes(value: float) -> float | None:
        return round(value, 1) if math.isfinite(value) else None

    def ids(route: Sequence[int]) -> list[str]:
        return [str(delivery_point_ids[index - 1]) for index in route[1:]]

    baseline = _route_cost(matrix, baseline_route)
    final = _route_cost(matrix, final_route)
    observations = cast(list[dict[str, object]], live_traffic.get("observations", []))
    legs = [
        {
            "fromStop": "courier" if start == 0 else str(delivery_point_ids[start - 1]),
            "toStop": str(delivery_point_ids[end - 1]),
            "minutes": _leg_minutes(matrix[start][end]),
        }
        for start, end in zip(final_route, final_route[1:], strict=False)
    ]
    alternatives = [
        {
            "step": name,
            "minutes": minutes(value),
            "deltaMinutes": minutes(value - final) if math.isfinite(value - final) else None,
        }
        for name, value in (
            ("nearestNeighbor", baseline),
            ("twoOpt", _route_cost(matrix, two_opt_route)),
            ("orOpt", or_opt_duration),
            ("refine", refined.cost),
        )
    ]
    return {
        "legs": legs,
        "alternatives": alternatives,
        "ladder": [
            {"step": "nearestNeighbor", "minutes": minutes(baseline)},
            {"step": "twoOpt", "minutes": minutes(_route_cost(matrix, two_opt_route))},
            {"step": "orOpt", "minutes": minutes(or_opt_duration)},
            {
                "step": "refine",
                "minutes": minutes(refined.cost),
                "kind": refined.kind,
                "provenOptimal": refined.proven_optimal,
                "lowerBoundMinutes": (
                    minutes(refined.lower_bound) if refined.lower_bound is not None else None
                ),
            },
            {
                "step": "aiOrder",
                "minutes": minutes(final),
                "accepted": bool(ai_order.get("accepted")),
                "proposed": bool(ai_order.get("proposed")),
            },
        ],
        "baselineMinutes": minutes(baseline),
        "optimizedMinutes": minutes(final),
        "savedMinutes": minutes(baseline - final) if math.isfinite(baseline - final) else None,
        "baselineOrder": ids(baseline_route),
        "optimizedOrder": ids(final_route),
        "baselineGeometry": baseline_geometry,
        "trafficPoints": [
            {
                "point": item.get("point"),
                "level": item.get("level"),
                "multiplier": item.get("trafficMultiplier"),
                "closed": bool(item.get("roadClosure")),
                "observedAt": item.get("observedAt"),
            }
            for item in observations
            if item.get("point") is not None
        ],
        "tomtom": {
            "configured": bool(live_traffic.get("configured")),
            "applied": bool(live_traffic.get("applied")),
            "sampled": live_traffic.get("sampledSegments", 0),
            "adjusted": live_traffic.get("adjustedSegments", 0),
            "closed": live_traffic.get("closedSegments", 0),
            "segments": live_traffic.get("routeSegments", 0),
        },
        "groq": {
            "trafficSource": traffic_ai.get("source"),
            "trafficApplied": bool(traffic_ai.get("applied")),
            "model": traffic_ai.get("model"),
            "orderProposed": bool(ai_order.get("proposed")),
            "orderAccepted": bool(ai_order.get("accepted")),
            "orderSavingMinutes": ai_order.get("savingMinutes"),
            "orderReason": ai_order.get("reason"),
        },
        "computedAt": computed_at.replace(tzinfo=UTC).isoformat(),
    }


def _bogota_hour(observed_utc: datetime) -> int:
    """Hora local de Bogotá (UTC-5) de una medición en UTC; el perfil horario usa hora local."""
    return (observed_utc + _BOGOTA_OFFSET).hour


async def _persist_traffic_history(
    session: AsyncSession, route_id: UUID, live_traffic: Mapping[str, object]
) -> None:
    """Guarda mediciones por tramo y hora solo si el interruptor está encendido."""
    if not get_settings().traffic_history_enabled:
        return
    rows: list[TrafficObservation] = []
    for item in cast(list[dict[str, object]], live_traffic.get("observations", [])):
        point = item.get("point")
        if not isinstance(point, list) or len(point) != 2:
            continue
        observed = datetime.fromisoformat(str(item["observedAt"])).replace(tzinfo=None)
        rows.append(
            TrafficObservation(
                route_id=route_id,
                from_stop=str(item["fromStopId"]),
                to_stop=str(item["toStopId"]),
                multiplier=cast(float | None, item.get("trafficMultiplier")),
                level=str(item.get("level") or "FLUIDO"),
                road_closure=bool(item.get("roadClosure")),
                point_lng=float(point[0]),
                point_lat=float(point[1]),
                hour_of_day=_bogota_hour(observed),
                observed_at=observed,
            )
        )
    if not rows:
        return
    try:
        # Punto de guardado: un fallo del historial no debe deshacer la ruta recién creada.
        async with session.begin_nested():
            session.add_all(rows)
    except SQLAlchemyError as error:
        _LOGGER.warning(
            "No se pudo guardar el historial de tráfico; la ruta se conserva",
            extra={"error_type": type(error).__name__},
        )


def _leg_minutes(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 1)


def _traffic_level(multiplier: float | None, closed: bool) -> str:
    """Misma escala que el mapa: de fluido a cerrado según cuánto se alarga el tramo."""
    if closed:
        return "CERRADO"
    if multiplier is None or not math.isfinite(multiplier) or multiplier < 1.15:
        return "FLUIDO"
    if multiplier < 1.5:
        return "MODERADO"
    if multiplier < 2.0:
        return "ALTO"
    return "MUY_ALTO"


async def create_optimized_route(
    session: AsyncSession,
    plan: OptimizedRoutePlan,
    actor: EventActor = SYSTEM_ACTOR,
) -> RouteOperationResult:
    results = await create_optimized_routes_batch(session, [plan], actor)
    return results[0]


async def create_optimized_routes_batch(
    session: AsyncSession,
    plans: Sequence[OptimizedRoutePlan],
    actor: EventActor = SYSTEM_ACTOR,
) -> list[RouteOperationResult]:
    if not plans:
        raise RoutePlanningError("Se requiere al menos una asignación para aplicar.", 400)
    courier_ids = [plan.courier_id for plan in plans]
    if len(set(courier_ids)) != len(courier_ids):
        raise RouteConflictError("Cada courier solo puede aparecer una vez en assignments.")

    # Preflight SELECTs used an implicit read transaction; restart before locking/persisting.
    await session.rollback()
    results: list[RouteOperationResult] = []
    async with session.begin():
        repository = RouteRepository(session)
        notification_repository = NotificationRepository(session)
        couriers_by_id = await repository.lock_couriers(courier_ids)
        if len(couriers_by_id) != len(courier_ids):
            raise RoutePlanningError("Un courier solicitado ya no existe.", 404)
        if any(
            couriers_by_id[courier_id].status != CourierStatus.AVAILABLE
            for courier_id in courier_ids
        ):
            raise RouteConflictError("Un courier ya no está AVAILABLE.")
        planned_at = datetime.now(UTC).replace(tzinfo=None)
        for plan in plans:
            _validate_courier_position(couriers_by_id[plan.courier_id], plan, planned_at)
        if await repository.has_active_route(courier_ids, _ACTIVE_ROUTE_STATUSES):
            raise RouteConflictError("Un courier ya tiene una ruta planificada o en progreso.")

        for plan in plans:
            route = Route(
                courier_id=plan.courier_id,
                status=RouteStatus.PLANNED,
                estimated_duration_minutes=plan.estimated_duration_minutes,
                estimated_distance_meters=plan.estimated_distance_meters,
                baseline_duration_minutes=plan.baseline_duration_minutes,
                baseline_distance_meters=plan.baseline_distance_meters,
                geometry=plan.geometry,
                geometry_provider=plan.geometry_provider,
                optimized_at=planned_at,
            )
            repository.add_route(route)
            await session.flush()
            await _assign_delivery_points(
                session,
                route.id,
                plan.ordered_delivery_point_ids,
                expected_coordinates=dict(plan.planned_delivery_point_coordinates),
                allowed_statuses=(DeliveryPointStatus.PENDING,),
            )
            assigned_points = await repository.get_delivery_points(
                plan.ordered_delivery_point_ids
            )
            DeliveryEventRecorder(session).assigned(
                assigned_points, route.id, plan.courier_id, actor
            )
            queued_by_channel = {
                CustomerNotificationChannel.EMAIL: 0,
                CustomerNotificationChannel.SMS: 0,
            }
            for point in assigned_points:
                for channel in await enqueue_order_assigned_notification(session, point, route.id):
                    queued_by_channel[channel] += 1
            notification_repository.add(
                Notification(
                    kind=NotificationKind.ROUTE_CREATED,
                    recipient_id=str(plan.courier_id),
                    title="Ruta asignada",
                    message=(
                        f"Se asignó una ruta con "
                        f"{len(plan.ordered_delivery_point_ids)} paradas."
                    ),
                )
            )
            await session.flush()
            saved_route = await repository.load_route(route.id)
            if saved_route is None:
                raise RuntimeError("La ruta recién creada no pudo volver a consultarse.")
            ROUTE_OPTIMIZATIONS.put(saved_route.id, plan.optimization)
            await _persist_traffic_history(session, saved_route.id, plan.live_traffic)
            results.append(
                RouteOperationResult(
                    saved_route,
                    plan.ordered_delivery_point_ids,
                    plan.matrix_source,
                    plan.warning,
                    plan.traffic_ai,
                    sum(queued_by_channel.values()),
                    tuple(queued_by_channel.items()),
                    plan.live_traffic,
                )
            )
    return results


async def recalculate_route(
    session: AsyncSession,
    route_id: UUID,
    plan: OptimizedRoutePlan,
    previous_active_ids: Sequence[UUID],
) -> RouteOperationResult:
    await session.rollback()
    async with session.begin():
        repository = RouteRepository(session)
        notification_repository = NotificationRepository(session)
        route = await repository.lock_route(route_id)
        if route is None:
            raise RoutePlanningError("La ruta no existe.", 404)
        if route.status not in _ACTIVE_ROUTE_STATUSES:
            raise RouteConflictError("Solo se pueden recalcular rutas planificadas o en progreso.")
        if route.courier_id != plan.courier_id:
            raise RouteConflictError("La ruta cambió de repartidor mientras se recalculaba.")

        courier = await repository.get_courier(route.courier_id, for_update=True)
        if courier is None:
            raise RoutePlanningError("El courier de la ruta ya no existe.", 404)
        _validate_courier_position(courier, plan, datetime.now(UTC).replace(tzinfo=None))

        all_route_points = await repository.lock_route_delivery_points(route_id)
        active_points = [
            point for point in all_route_points if point.status in _ACTIVE_POINT_STATUSES
        ]
        currently_active_ids = {point.id for point in active_points}
        if currently_active_ids != set(previous_active_ids):
            raise RouteConflictError("Un pedido cambió mientras se recalculaba la ruta.")
        snapshot = _build_route_revision_snapshot(route, active_points)
        repository.add_route_revision(
            RouteRevision(
                route_id=route_id,
                revision_number=await repository.next_route_revision_number(route_id),
                snapshot=snapshot,
            )
        )
        await session.flush()
        await repository.prune_route_revisions(route_id, MAX_ROUTE_REVISIONS)
        next_sequence_index = max(
            (
                point.sequence_index
                for point in all_route_points
                if point.status in _TERMINAL_POINT_STATUSES
                and point.sequence_index is not None
            ),
            default=-1,
        ) + 1

        # Las paradas terminales conservan su vínculo e índice como historial de la ruta.
        await repository.release_active_delivery_points(
            route_id,
            _ACTIVE_POINT_STATUSES,
            reset_to_pending=False,
        )
        await _assign_delivery_points(
            session,
            route_id,
            plan.ordered_delivery_point_ids,
            expected_coordinates=dict(plan.planned_delivery_point_coordinates),
            allowed_statuses=_ACTIVE_POINT_STATUSES,
            sequence_start=next_sequence_index,
        )
        optimized_at = datetime.now(UTC).replace(tzinfo=None)
        route.estimated_duration_minutes = plan.estimated_duration_minutes
        route.estimated_distance_meters = plan.estimated_distance_meters
        route.baseline_duration_minutes = plan.baseline_duration_minutes
        route.baseline_distance_meters = plan.baseline_distance_meters
        route.geometry = plan.geometry
        route.geometry_provider = plan.geometry_provider
        route.updated_at = optimized_at
        route.optimized_at = optimized_at
        notification_repository.add(
            Notification(
                kind=NotificationKind.ROUTE_RECALCULATED,
                recipient_id=str(route.courier_id),
                title="Ruta recalculada",
                message=(
                    f"La ruta ahora tiene {len(plan.ordered_delivery_point_ids)} paradas activas."
                ),
            )
        )
        await session.flush()
        updated = await repository.load_route(route_id)
        if updated is None:
            raise RuntimeError("La ruta recalculada no pudo volver a consultarse.")
        ROUTE_OPTIMIZATIONS.put(updated.id, plan.optimization)
        await _persist_traffic_history(session, updated.id, plan.live_traffic)
        result = RouteOperationResult(
            updated,
            plan.ordered_delivery_point_ids,
            plan.matrix_source,
            plan.warning,
            plan.traffic_ai,
            live_traffic=plan.live_traffic,
        )
    return result


async def undo_route_recalculation(
    session: AsyncSession,
    route_id: UUID,
) -> RouteUndoResult:
    """Restaura el snapshot persistido más reciente de una ruta activa, en orden LIFO."""
    await session.rollback()
    async with session.begin():
        repository = RouteRepository(session)
        route = await repository.lock_route_with_delivery_points(route_id)
        if route is None:
            raise RoutePlanningError("La ruta no existe.", 404)
        if route.status not in _ACTIVE_ROUTE_STATUSES:
            raise RouteConflictError("Solo se puede restaurar una ruta planificada o en progreso.")

        revisions = await repository.list_route_revisions(route_id)
        history: Stack[RouteRevision] = Stack()
        for revision in revisions:
            history.push(revision)
        revision_to_restore = history.pop()
        if revision_to_restore is None:
            raise RouteConflictError("La ruta no tiene un recálculo anterior para restaurar.")
        snapshot = _read_route_revision_snapshot(revision_to_restore.snapshot)

        points = await repository.lock_route_delivery_points(route_id)
        active_points = [point for point in points if point.status in _ACTIVE_POINT_STATUSES]
        points_by_id = {point.id: point for point in active_points}
        snapshot_ids = {point_id for point_id, _sequence in snapshot.active_stops}
        if not snapshot_ids.issubset(points_by_id):
            raise RouteConflictError(
                "No se puede restaurar: una parada anterior ya cambió de estado o salió de la ruta."
            )

        released_points = [
            point for point in active_points if point.id not in snapshot_ids
        ]
        if any(point.status != DeliveryPointStatus.PENDING for point in released_points):
            raise RouteConflictError(
                "No se puede restaurar: una parada agregada ya inició su entrega."
            )

        # Liberar índices primero evita colisiones con la restricción única route_id/sequence_index.
        for point in active_points:
            point.sequence_index = None
        for point in released_points:
            point.route_id = None
        await session.flush()

        for point_id, sequence_index in snapshot.active_stops:
            points_by_id[point_id].sequence_index = sequence_index
        restored_at = datetime.now(UTC).replace(tzinfo=None)
        route.estimated_duration_minutes = snapshot.estimated_duration_minutes
        route.estimated_distance_meters = snapshot.estimated_distance_meters
        route.baseline_duration_minutes = snapshot.baseline_duration_minutes
        route.baseline_distance_meters = snapshot.baseline_distance_meters
        route.geometry = snapshot.geometry
        route.geometry_provider = snapshot.geometry_provider
        route.updated_at = restored_at
        route.optimized_at = restored_at
        await repository.delete_route_revision(revision_to_restore.id)
        await session.flush()

        remaining_revisions = await repository.list_route_revisions(route_id)
        updated = await repository.load_route(route_id)
        if updated is None:
            raise RuntimeError("La ruta restaurada no pudo volver a consultarse.")
        result = RouteUndoResult(
            route=updated,
            can_undo=bool(remaining_revisions),
            released_delivery_point_ids=tuple(point.id for point in released_points),
        )
    return result


async def update_route_status(
    session: AsyncSession,
    route_id: UUID,
    next_status: RouteStatus,
    actor: EventActor = SYSTEM_ACTOR,
) -> Route:
    await session.rollback()
    async with session.begin():
        repository = RouteRepository(session)
        route = await repository.lock_route_with_delivery_points(route_id)
        if route is None:
            raise RoutePlanningError("La ruta no existe.", 404)
        _validate_transition(route.status, next_status)
        if next_status == RouteStatus.COMPLETED and any(
            point.status in _ACTIVE_POINT_STATUSES for point in route.delivery_points
        ):
            raise RouteConflictError("No se puede completar una ruta con pedidos pendientes.")

        now = datetime.now(UTC).replace(tzinfo=None)
        recorder = DeliveryEventRecorder(session)
        if next_status == RouteStatus.CANCELLED:
            recorder.released(
                [p for p in route.delivery_points if p.status in _ACTIVE_POINT_STATUSES],
                actor,
                "route cancelled",
            )
            await repository.release_active_delivery_points(
                route_id,
                _ACTIVE_POINT_STATUSES,
                reset_to_pending=True,
            )
        elif next_status == RouteStatus.IN_PROGRESS:
            recorder.started(
                [p for p in route.delivery_points if p.status == DeliveryPointStatus.PENDING],
                actor,
            )
            await repository.mark_pending_delivery_points_en_route(route_id)
            if route.started_at is None:
                route.started_at = now

        route.status = next_status
        if next_status in {RouteStatus.COMPLETED, RouteStatus.CANCELLED}:
            route.completed_at = route.completed_at or now
            await repository.delete_route_revisions(route_id)
            courier = await repository.get_courier(route.courier_id, for_update=True)
            if courier is not None:
                courier.status = CourierStatus.AVAILABLE
        elif next_status == RouteStatus.IN_PROGRESS:
            courier = await repository.get_courier(route.courier_id, for_update=True)
            if courier is not None:
                courier.status = CourierStatus.ON_ROUTE
        await session.flush()
        updated = await repository.load_route(route_id)
        if updated is None:
            raise RuntimeError("La ruta actualizada no pudo volver a consultarse.")
    return updated


async def _assign_delivery_points(
    session: AsyncSession,
    route_id: UUID,
    ordered_ids: Sequence[UUID],
    *,
    expected_coordinates: Mapping[UUID, Coordinate],
    allowed_statuses: Sequence[DeliveryPointStatus],
    sequence_start: int = 0,
) -> None:
    repository = RouteRepository(session)
    for sequence_index, point_id in enumerate(ordered_ids, start=sequence_start):
        expected_coordinate = expected_coordinates.get(point_id)
        if expected_coordinate is None:
            raise RoutePlanningError(
                f"El plan no contiene las coordenadas originales del punto {point_id}."
            )
        longitude, latitude = expected_coordinate
        assigned = await repository.try_assign_delivery_point(
            route_id=route_id,
            point_id=point_id,
            allowed_statuses=allowed_statuses,
            expected_latitude=latitude,
            expected_longitude=longitude,
            sequence_index=sequence_index,
        )
        if not assigned:
            raise RouteConflictError(
                f"El delivery point {point_id} cambió mientras se guardaba o calculaba la ruta."
            )


def _validate_courier_position(
    courier: Courier,
    plan: OptimizedRoutePlan,
    now: datetime,
) -> None:
    current_location = (
        (courier.current_lng, courier.current_lat)
        if courier.current_lng is not None and courier.current_lat is not None
        else None
    )
    if current_location != plan.planned_courier_location:
        raise RouteConflictError(
            "La ubicación del repartidor cambió mientras se calculaba la ruta."
        )
    if not is_courier_location_fresh(courier.last_location_at, now):
