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
