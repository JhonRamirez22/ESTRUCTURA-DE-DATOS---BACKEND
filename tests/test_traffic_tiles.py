from __future__ import annotations

import base64
import math

import httpx
import pytest

from app.core.traffic.tiles import validate_traffic_tile
from app.services.traffic import (
    TrafficNoCoverageError,
    TrafficProviderError,
    TrafficRateLimitError,
    TrafficService,
    TrafficTimeoutError,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "ScLbtAAAAABJRU5ErkJggg=="
)


def pasto_tile(z: int = 14) -> tuple[int, int, int]:
    size = 2**z
    return (
        z,
        int((-77.28 + 180) / 360 * size),
        int(
            (1 - math.asinh(math.tan(math.radians(1.21))) / math.pi) / 2 * size,
        ),
    )


@pytest.mark.parametrize(
    "tile", [(0, 0, 0), (23, 0, 0), (14, -1, 10), (14, 1, 1), (14, 16384, 10), (True, 1, 1)]
)
def test_proxy_rejects_invalid_zoom_indices_and_tiles_outside_pasto(
    tile: tuple[int, int, int],
) -> None:
    with pytest.raises(ValueError):
        validate_traffic_tile(*tile)


@pytest.mark.asyncio
async def test_png_cache_hit_miss_and_exact_ttl_boundary() -> None:
    calls: list[httpx.Request] = []
    clock = [0.0]

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "/relative0/" in request.url.path
        assert request.url.params["tileSize"] == "256"
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    service = TrafficService(
        api_key="fake-test-key", transport=httpx.MockTransport(respond), monotonic=lambda: clock[0]
    )
    try:
        first = await service.get_flow_tile(*pasto_tile())
        clock[0] = 89.999
        cached = await service.get_flow_tile(*pasto_tile())
        assert first == cached and len(calls) == 1
