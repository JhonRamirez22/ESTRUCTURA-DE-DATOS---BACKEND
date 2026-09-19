"""Ajuste acotado del ETA con una observación TomTom del siguiente tramo vial."""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from uuid import UUID

from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.route_sampling import sample_route_leg_points
from app.core.traffic.tomtom import TrafficCacheStatus, TrafficData

_LOGGER = logging.getLogger(__name__)
MIN_TOMTOM_CONFIDENCE = 0.3
CUSTOMER_ETA_TRAFFIC_CACHE_SECONDS = 60
CUSTOMER_ETA_TRAFFIC_CACHE_MAX_ENTRIES = 512
TrafficEstimateStatus = TrafficCacheStatus | Literal["NOT_CONFIGURED", "UNAVAILABLE"]


class RouteProvider(Protocol):
    async def get_route(self, coordinates: list[Coordinate]) -> RouteResult: ...


class TrafficProvider(Protocol):
    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData: ...


@dataclass(frozen=True, slots=True)
class FirstLegTrafficEstimate:
    duration_minutes: float | None
    distance_meters: float | None
    road_route: RouteResult | None
    configured: bool
    applied: bool
    closed: bool
    status: TrafficEstimateStatus
    warning: str | None


@dataclass(frozen=True, slots=True)
class _TrafficCacheEntry:
    expires_at: float
    data: TrafficData


_traffic_cache: OrderedDict[tuple[UUID, UUID], _TrafficCacheEntry] = OrderedDict()
# Caché local del proceso; en pnpm dev puede reiniciarse con hot reload y no es persistente.
# TODO: agrupar misses concurrentes de una ruta/parada; no se requiere para un solo despachador.


async def estimate_first_leg_with_traffic(
    *,
    route_id: UUID,
    next_stop_id: UUID,
    start: Coordinate,
