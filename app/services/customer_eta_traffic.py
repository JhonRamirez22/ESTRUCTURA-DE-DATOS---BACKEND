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
    end: Coordinate,
    base_duration_minutes: float | None,
    base_distance_meters: float | None,
    route_provider: RouteProvider,
    traffic_provider: TrafficProvider | None,
    road_route: RouteResult | None = None,
    require_road_route: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> FirstLegTrafficEstimate:
    """Use TomTom on one verified leg; preserve base road ETA on provider failures."""
    needs_geometry = traffic_provider is not None or require_road_route
    if road_route is None and needs_geometry:
        try:
            candidate = await route_provider.get_route([start, end])
            if candidate["geometry_provider"] in {"OSRM", "ORS"}:
                road_route = candidate
        except Exception as error:
            _LOGGER.warning(
                "No se pudo obtener geometría vial para el ETA del cliente",
                extra={"error_type": type(error).__name__},
            )

    duration = (
        road_route["duration_minutes"] if road_route is not None else base_duration_minutes
    )
    distance = road_route["distance_meters"] if road_route is not None else base_distance_meters

    if traffic_provider is None:
        return FirstLegTrafficEstimate(
            duration,
            distance,
            road_route,
            configured=False,
            applied=False,
            closed=False,
            status="NOT_CONFIGURED",
            warning="TomTom no está configurado; el ETA usa tiempos viales base.",
        )
    if road_route is None:
        return _unavailable_estimate(
            duration,
            distance,
            None,
            "No se pudo verificar el primer tramo; se conserva el ETA vial base.",
        )

    samples = sample_route_leg_points(road_route["geometry"], [start, end])
    if len(samples) != 1:
        return _unavailable_estimate(
            duration,
            distance,
            road_route,
            "No se pudo ubicar un punto de tráfico en el primer tramo; "
            "se conserva el ETA vial base.",
        )

    try:
        traffic = await _get_flow(
            route_id,
            next_stop_id,
            samples[0],
            traffic_provider,
            monotonic,
        )
    except Exception as error:
        _LOGGER.warning(
            "TomTom no pudo ajustar el ETA del primer tramo",
            extra={"error_type": type(error).__name__},
        )
        return _unavailable_estimate(
            duration,
            distance,
            road_route,
            "TomTom no respondió; se conserva el ETA vial base.",
        )

    if traffic.road_closure:
        return FirstLegTrafficEstimate(
            None,
            distance,
            road_route,
            configured=True,
            applied=True,
            closed=True,
            status=traffic.cache_status,
            warning=(
                "TomTom reporta un posible cierre en el siguiente tramo; "
                "el ETA no está disponible."
            ),
        )
    multiplier = traffic.travel_time_multiplier
    if (
        traffic.confidence < MIN_TOMTOM_CONFIDENCE
        or multiplier is None
        or not math.isfinite(multiplier)
        or multiplier <= 0
        or duration is None
    ):
        return FirstLegTrafficEstimate(
            duration,
            distance,
            road_route,
            configured=True,
            applied=False,
            closed=False,
            status=traffic.cache_status,
            warning="TomTom no entregó un factor confiable; se conserva el ETA vial base.",
        )

    warning = (
        "Se ajustó solo el siguiente tramo con tráfico TomTom; los tramos posteriores usan "
        "tiempos viales base."
    )
    if traffic.cache_status == "STALE":
        warning = "TomTom usa un dato reciente en caché; " + warning[0].lower() + warning[1:]
    return FirstLegTrafficEstimate(
        duration * multiplier,
        distance,
        road_route,
        configured=True,
        applied=True,
        closed=False,
        status=traffic.cache_status,
        warning=warning,
    )


async def _get_flow(
    route_id: UUID,
    next_stop_id: UUID,
    coordinate: Coordinate,
    provider: TrafficProvider,
    monotonic: Callable[[], float],
) -> TrafficData:
    key = (route_id, next_stop_id)
    now = monotonic()
    cached = _traffic_cache.get(key)
    if cached is not None and cached.expires_at > now:
        _traffic_cache.move_to_end(key)
        status: TrafficCacheStatus = (
            "STALE" if cached.data.cache_status == "STALE" else "CACHE"
        )
        return replace(cached.data, cache_status=status)
    if cached is not None:
        del _traffic_cache[key]

    traffic = await provider.get_flow_segment(coordinate)
    _traffic_cache[key] = _TrafficCacheEntry(
        expires_at=now + CUSTOMER_ETA_TRAFFIC_CACHE_SECONDS,
        data=traffic,
    )
    _traffic_cache.move_to_end(key)
    while len(_traffic_cache) > CUSTOMER_ETA_TRAFFIC_CACHE_MAX_ENTRIES:
        _traffic_cache.popitem(last=False)
    return traffic


def _unavailable_estimate(
    duration: float | None,
    distance: float | None,
    road_route: RouteResult | None,
    warning: str,
) -> FirstLegTrafficEstimate:
    return FirstLegTrafficEstimate(
        duration,
        distance,
        road_route,
        configured=True,
        applied=False,
        closed=False,
        status="UNAVAILABLE",
        warning=warning,
    )


def clear_customer_eta_traffic_cache() -> None:
    """Limpia el caché acotado en memoria para pruebas deterministas."""
    _traffic_cache.clear()
