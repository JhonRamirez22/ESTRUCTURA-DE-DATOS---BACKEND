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
