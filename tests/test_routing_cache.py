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
        "geometry_provider": provider,  # type: ignore[typeddict-item]
        "distance_meters": 1_500,
        "duration_minutes": 6,
        "snap_distances_meters": [4, 4],
    }


@pytest.fixture(autouse=True)
def _clear_module_caches() -> None:
    clear_matrix_cache()
    clear_route_cache()


@pytest.mark.asyncio
async def test_matrix_cache_hit_rounding_ttl_and_ordered_key() -> None:
    current_time = 100.0
    client = _MatrixClient(MATRIX)
    provider = CachedMatrixProvider(client, ttl_seconds=5, now=lambda: current_time)
    jittered = [(-77.2811009, 1.2136009), (-77.2800009, 1.2140009)]

    first = await provider.get_duration_matrix(COORDINATES)
    second = await provider.get_duration_matrix(jittered)
    assert first["source"] == second["source"] == "osrm"
    assert client.calls == 1
    assert build_matrix_cache_key(COORDINATES) == build_matrix_cache_key(jittered)
    reversed_coordinates = list(reversed(COORDINATES))
    assert build_matrix_cache_key(COORDINATES) != build_matrix_cache_key(reversed_coordinates)

    current_time = 106.0
    await provider.get_duration_matrix(COORDINATES)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_matrix_cache_evicts_least_recently_used_with_doubly_linked_list() -> None:
    client = _MatrixClient(MATRIX)
    provider = CachedMatrixProvider(client, max_entries=2)
    first = COORDINATES
    second: list[Coordinate] = [(-77.27, 1.2136), (-77.269, 1.214)]
    third: list[Coordinate] = [(-77.26, 1.2136), (-77.259, 1.214)]

    await provider.get_duration_matrix(first)
    await provider.get_duration_matrix(second)
    await provider.get_duration_matrix(first)
    await provider.get_duration_matrix(third)
    await provider.get_duration_matrix(first)
    await provider.get_duration_matrix(second)

    assert client.calls == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [OsrmRateLimitError("limit"), OsrmTimeoutError(10)],
)
async def test_matrix_cache_uses_uncached_fallback_for_recoverable_errors(
    error: Exception,
) -> None:
    client = _MatrixClient(error)
    provider = CachedMatrixProvider(client)
    result = await provider.get_duration_matrix(COORDINATES)
    assert result["source"] == "euclidean-fallback"
    assert result["warning"]
    assert result["durations_minutes"][0][0] == 0
    assert FALLBACK_MAX_SPEED_KMH == 30

    client.result = MATRIX
    second = await provider.get_duration_matrix(COORDINATES)
    assert second["source"] == "osrm"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_matrix_cache_propagates_nonrecoverable_provider_errors() -> None:
    error = OsrmClientError("invalid request", status=400)
    provider = CachedMatrixProvider(_MatrixClient(error))
    with pytest.raises(OsrmClientError, match="invalid request"):
        await provider.get_duration_matrix(COORDINATES)


@pytest.mark.asyncio
async def test_route_cache_hits_expires_and_preserves_provider_and_order() -> None:
    current_time = 10.0
    client = _RouteClient(_route_result())
    provider = CachedRouteProvider(client, ttl_seconds=5, now=lambda: current_time)
    await provider.get_route(COORDINATES)
    await provider.get_route(COORDINATES)
    assert client.calls == 1

    current_time = 16.0
    await provider.get_route(COORDINATES)
    assert client.calls == 2
    assert build_route_cache_key("OSRM", COORDINATES) != build_route_cache_key(
        "OSRM", list(reversed(COORDINATES))
    )
    assert build_route_cache_key("ORS", COORDINATES) != build_route_cache_key("OSRM", COORDINATES)


@pytest.mark.asyncio
async def test_route_cache_deduplicates_concurrent_requests() -> None:
    client = _RouteClient(_route_result())
    client.started = asyncio.Event()
    client.release = asyncio.Event()
    provider = CachedRouteProvider(client)
    first = asyncio.create_task(provider.get_route(COORDINATES))
    await client.started.wait()
    second = asyncio.create_task(provider.get_route(COORDINATES))
    client.release.set()

    assert await asyncio.gather(first, second) == [_route_result(), _route_result()]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_route_cache_rejects_unexpected_provider_without_caching() -> None:
    client = _RouteClient(_route_result(provider="ORS"))
    provider = CachedRouteProvider(client, provider="OSRM")
    with pytest.raises(ValueError, match="Se esperaba geometría OSRM"):
        await provider.get_route(COORDINATES)
    assert client.calls == 1
    assert build_route_cache_key("OSRM", COORDINATES)
