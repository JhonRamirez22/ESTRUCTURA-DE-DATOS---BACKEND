from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from app.core.routing.cached_matrix_provider import (
    FALLBACK_MAX_SPEED_KMH,
    CachedMatrixProvider,
    build_matrix_cache_key,
    clear_matrix_cache,
)
from app.core.routing.cached_route_provider import (
    CachedRouteProvider,
    build_route_cache_key,
    clear_route_cache,
)
from app.core.routing.contracts import Coordinate, MatrixResult, RouteResult
from app.core.routing.osrm_client import (
    OsrmClientError,
    OsrmRateLimitError,
    OsrmTimeoutError,
)

COORDINATES: list[Coordinate] = [(-77.2811, 1.2136), (-77.28, 1.214)]
MATRIX: MatrixResult = {
    "durations_minutes": [[0, 5], [6, 0]],
    "distances_meters": [[0, 1_500], [1_800, 0]],
}


class _MatrixClient:
    def __init__(self, result: MatrixResult | Exception) -> None:
        self.result = result
        self.calls = 0

    async def get_duration_matrix(self, _coordinates: Sequence[Coordinate]) -> MatrixResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _RouteClient:
    def __init__(self, result: RouteResult) -> None:
        self.result = result
        self.calls = 0
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def get_route(self, _coordinates: Sequence[Coordinate]) -> RouteResult:
        self.calls += 1
        if self.started is not None and self.release is not None:
            self.started.set()
            await self.release.wait()
        return self.result


def _route_result(provider: str = "OSRM") -> RouteResult:
    return {
        "geometry": {"type": "LineString", "coordinates": list(COORDINATES)},
