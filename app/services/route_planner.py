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
                RouteTrafficAiRequest(tuple(candidates), current_time.replace(tzinfo=UTC))
            )
            supported, ignored_count = filter_route_traffic_adjustments(
                candidates, advice.adjustments
            )
            optimization_matrix = apply_route_traffic_adjustments(
                baseline_matrix, stop_ids, supported
            )
            warnings: list[str] = []
            if supported:
                warnings.append(
                    "El factor de IA es una estimación; el proveedor vial no proporciona "
                    "tráfico en vivo."
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

    optimized_route = two_opt(optimization_matrix, nearest_neighbor(optimization_matrix, 0))
    baseline_duration = _route_cost(baseline_matrix, baseline_route)
    optimized_duration = _route_cost(optimization_matrix, optimized_route)
    if not math.isfinite(baseline_duration) or not math.isfinite(optimized_duration):
        raise RouteConflictError("No existe un camino conectado que cubra todos los pedidos.")

    if traffic_provider is None:
        initial_traffic_warning = (
            "TomTom Traffic no está configurado; la ruta usa los tiempos viales de base."
        )
    elif not has_road_times:
        initial_traffic_warning = (
            "TomTom no se consultó porque la matriz no contiene tiempos viales verificados."
        )
    else:
        initial_traffic_warning = (
            "No se pudo muestrear tráfico vial; se conservan los tiempos de base."
        )
    live_traffic: dict[str, object] = {
        "source": "osrm-baseline",
        "configured": traffic_provider is not None,
        "applied": False,
        "routeSegments": max(0, len(optimized_route) - 1),
        "sampledSegments": 0,
        "adjustedSegments": 0,
        "closedSegments": 0,
        "coverageRatio": 0.0,
        "warning": initial_traffic_warning,
    }
    ordered_coordinates = [coordinates[index] for index in optimized_route]
    route_result = await route_provider.get_route(ordered_coordinates)
    geometry_provider = route_result["geometry_provider"]
    if geometry_provider not in {"OSRM", "ORS"}:
        raise RoutePlanningError(
            "El proveedor no verificó una geometría vial; no se guardará una línea recta.", 502
        )
    if traffic_provider is not None and has_road_times:
        optimization_matrix, live_traffic = await _apply_live_traffic_to_route(
            traffic_provider,
            optimization_matrix,
            optimized_route,
            ordered_coordinates,
            route_result["geometry"],
            baseline_matrix=baseline_matrix,
        )
        if live_traffic["applied"]:
            optimized_route = two_opt(
                optimization_matrix,
                nearest_neighbor(optimization_matrix, 0),
            )
            optimized_duration = _route_cost(optimization_matrix, optimized_route)
            if not math.isfinite(optimized_duration):
                raise RouteConflictError(
                    "TomTom reportó cierres que impiden conectar todas las paradas con "
                    "los tramos disponibles. Revisa la ruta antes de asignarla."
                )
            final_coordinates = [coordinates[index] for index in optimized_route]
            if final_coordinates != ordered_coordinates:
                route_result = await route_provider.get_route(final_coordinates)
                geometry_provider = route_result["geometry_provider"]
                if geometry_provider not in {"OSRM", "ORS"}:
                    raise RoutePlanningError(
                        "El proveedor no verificó una geometría vial; "
                        "no se guardará una línea recta.",
                        502,
                    )
        else:
            traffic_ai["warning"] = (
                f"{traffic_ai['warning']} {live_traffic['warning']}"
            ).strip()

    ordered_ids = tuple(delivery_point_ids[index - 1] for index in optimized_route[1:])
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
            if live_traffic["applied"]
            else route_result["duration_minutes"]
        ),
        estimated_distance_meters=route_result["distance_meters"],
        baseline_duration_minutes=(
            baseline_duration if has_road_times else None
        ),
        baseline_distance_meters=baseline_distance if math.isfinite(baseline_distance) else None,
        geometry=route_result["geometry"],
        geometry_provider=RouteGeometryProvider(geometry_provider),
        matrix_source=str(matrix_source),
        warning=matrix_result.get("warning"),
        traffic_ai=traffic_ai,
        live_traffic=live_traffic,
    )


async def create_optimized_route(
    session: AsyncSession,
    plan: OptimizedRoutePlan,
) -> RouteOperationResult:
    results = await create_optimized_routes_batch(session, [plan])
    return results[0]


async def create_optimized_routes_batch(
    session: AsyncSession,
    plans: Sequence[OptimizedRoutePlan],
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
        if next_status == RouteStatus.CANCELLED:
            await repository.release_active_delivery_points(
                route_id,
                _ACTIVE_POINT_STATUSES,
                reset_to_pending=True,
            )
        elif next_status == RouteStatus.IN_PROGRESS:
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
        raise RouteConflictError(
