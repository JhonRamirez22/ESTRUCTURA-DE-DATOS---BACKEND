"""Estimación vial mínima para responder preguntas de llegada de una guía."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.core.location.courier_location import is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.metrics.estimate_eta import StopEta, estimate_stop_etas_from_matrix
from app.core.routing.cached_matrix_provider import MatrixProviderResult
from app.core.routing.contracts import Coordinate, RouteResult
from app.models import DeliveryPoint, DeliveryPointStatus, RouteStatus
from app.services.customer_eta_traffic import (
    TrafficEstimateStatus,
    TrafficProvider,
    estimate_first_leg_with_traffic,
)

_ACTIVE_ROUTE_STATUSES = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)
_TERMINAL_POINT_STATUSES = (DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED)


class MatrixProvider(Protocol):
    async def get_duration_matrix(
        self, coordinates: Sequence[Coordinate]
    ) -> MatrixProviderResult: ...


class RouteProvider(Protocol):
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult: ...


@dataclass(frozen=True, slots=True)
class CustomerEtaResult:
    estimated_minutes_from_now: float | None
    traffic_configured: bool
    traffic_applied: bool
    traffic_status: TrafficEstimateStatus
    traffic_warning: str | None


async def estimate_customer_order_eta(
    delivery_point: DeliveryPoint,
    matrix_provider: MatrixProvider,
    route_provider: RouteProvider,
    *,
    traffic_provider: TrafficProvider | None = None,
    now: datetime | None = None,
) -> CustomerEtaResult | None:
    """Return this order ETA and its partial-traffic provenance, never a guessed ETA."""
    route = delivery_point.route
    if (
        route is None
        or route.status not in _ACTIVE_ROUTE_STATUSES
        or delivery_point.status in _TERMINAL_POINT_STATUSES
    ):
        return None

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(UTC).replace(tzinfo=None)
    courier = route.courier
    if (
        courier.current_lat is None
        or courier.current_lng is None
        or not is_courier_location_fresh(courier.last_location_at, current_time)
        or not is_within_pasto_service_area(courier.current_lat, courier.current_lng)
    ):
        return None

    shared_position = (
        _round_coordinate(courier.current_lng),
        _round_coordinate(courier.current_lat),
    )
    remaining_points = sorted(
        (
            point
            for point in route.delivery_points
            if point.status not in _TERMINAL_POINT_STATUSES
        ),
        key=lambda point: (
            point.sequence_index if point.sequence_index is not None else 2**31
        ),
    )
    target_index = next(
        (index for index, point in enumerate(remaining_points) if point.id == delivery_point.id),
        None,
    )
    if target_index is None:
        return None

    coordinates: list[Coordinate] = [shared_position]
    coordinates.extend((point.lng, point.lat) for point in remaining_points)
    matrix_result = await matrix_provider.get_duration_matrix(coordinates)
    durations = [list(row) for row in matrix_result["durations_minutes"]]
    distances = matrix_result.get("distances_meters")
    copied_distances = [list(row) for row in distances] if distances is not None else None
    first_point = remaining_points[0]
    first_leg = await estimate_first_leg_with_traffic(
        route_id=route.id,
        next_stop_id=first_point.id,
        start=shared_position,
        end=(first_point.lng, first_point.lat),
        base_duration_minutes=(
            None
            if matrix_result["source"] == "euclidean-fallback"
            else durations[0][1]
        ),
        base_distance_meters=(
            None
            if matrix_result["source"] == "euclidean-fallback" or copied_distances is None
            else copied_distances[0][1]
        ),
        route_provider=route_provider,
        traffic_provider=traffic_provider,
        require_road_route=(
            target_index == 0 or matrix_result["source"] == "euclidean-fallback"
        ),
    )
    if first_leg.duration_minutes is not None:
        durations[0][1] = first_leg.duration_minutes
    else:
        durations[0][1] = None
    if copied_distances is not None and first_leg.distance_meters is not None:
        copied_distances[0][1] = first_leg.distance_meters

    if matrix_result["source"] == "euclidean-fallback" and (
        first_leg.road_route is None or target_index != 0
    ):
        return CustomerEtaResult(
            None,
            first_leg.configured,
            first_leg.applied,
            first_leg.status,
            first_leg.warning,
        )

    stop_etas: list[StopEta] = estimate_stop_etas_from_matrix(
        [str(point.id) for point in remaining_points],
        durations,
        copied_distances,
    )
    target_eta = stop_etas[target_index]
    return CustomerEtaResult(
        target_eta.estimated_minutes_from_now,
        first_leg.configured,
        first_leg.applied,
        first_leg.status,
        first_leg.warning,
    )


def _round_coordinate(value: float) -> float:
    return float(f"{value:.4f}")
