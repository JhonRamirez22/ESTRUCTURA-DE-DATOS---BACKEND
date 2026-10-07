"""Caché LRU de geometrías OSRM verificadas y deduplicación en vuelo."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Protocol, TypedDict

from app.core.routing.contracts import (
    Coordinate,
    GeometryProvider,
    RouteResult,
    validate_coordinates,
)

ROUTE_CACHE_TTL_SECONDS = 30
ROUTE_CACHE_MAX_ENTRIES = 100
_ROUTE_CACHE_COORDINATE_PRECISION = 4
_ROUTE_CACHE_SCHEMA = "verified-road-geometry-v2"


class RouteClient(Protocol):
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult: ...


class _RouteCacheEntry(TypedDict):
    expires_at: float
    result: RouteResult


_route_cache: OrderedDict[str, _RouteCacheEntry] = OrderedDict()
_in_flight_requests: dict[str, asyncio.Task[RouteResult]] = {}


class CachedRouteProvider:
    def __init__(
        self,
        client: RouteClient,
        provider: GeometryProvider = "OSRM",
        *,
        ttl_seconds: float = ROUTE_CACHE_TTL_SECONDS,
        max_entries: int = ROUTE_CACHE_MAX_ENTRIES,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds debe ser positivo y finito")
        if max_entries < 1:
            raise ValueError("max_entries debe ser positivo")
        self._client = client
        self._provider = provider
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._now = now

    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        validate_coordinates(coordinates)
        key = build_route_cache_key(self._provider, coordinates)
        cached = _route_cache.get(key)
        if cached is not None and cached["expires_at"] > self._now():
            _route_cache.move_to_end(key)
            return _copy_route_result(cached["result"])
        if cached is not None:
            del _route_cache[key]

        task = _in_flight_requests.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_and_cache(key, coordinates))
            _in_flight_requests[key] = task
            task.add_done_callback(lambda completed: _remove_finished_task(key, completed))

        return _copy_route_result(await asyncio.shield(task))

    async def _fetch_and_cache(
        self,
        key: str,
        coordinates: Sequence[Coordinate],
    ) -> RouteResult:
        result = await self._client.get_route(coordinates)
        if result["geometry_provider"] != self._provider:
            raise ValueError(
                f"Se esperaba geometría {self._provider}, se recibió {result['geometry_provider']}"
            )

        copied = _copy_route_result(result)
        _route_cache[key] = {"expires_at": self._now() + self._ttl_seconds, "result": copied}
        _route_cache.move_to_end(key)
        while len(_route_cache) > self._max_entries:
            _route_cache.popitem(last=False)
        return copied


def build_route_cache_key(
    provider: GeometryProvider,
    coordinates: Sequence[Coordinate],
) -> str:
    rounded = [
        [
            round(longitude, _ROUTE_CACHE_COORDINATE_PRECISION),
            round(latitude, _ROUTE_CACHE_COORDINATE_PRECISION),
        ]
        for longitude, latitude in coordinates
    ]
    return f"{_ROUTE_CACHE_SCHEMA}:{provider}:{rounded!r}"


def clear_route_cache() -> None:
    """Cancela requests de prueba y limpia la memoria del módulo."""
    for task in _in_flight_requests.values():
        task.cancel()
    _in_flight_requests.clear()
    _route_cache.clear()


def _remove_finished_task(key: str, completed: asyncio.Task[RouteResult]) -> None:
    if _in_flight_requests.get(key) is completed:
        del _in_flight_requests[key]
    if not completed.cancelled():
        completed.exception()


def _copy_route_result(result: RouteResult) -> RouteResult:
    geometry = result["geometry"]
    copied: RouteResult = {
        "geometry": {
            "type": "LineString",
            "coordinates": list(geometry["coordinates"]),
        },
        "geometry_provider": result["geometry_provider"],
        "distance_meters": result["distance_meters"],
        "duration_minutes": result["duration_minutes"],
    }
    if "snap_distances_meters" in result:
        copied["snap_distances_meters"] = list(result["snap_distances_meters"])
    return copied
