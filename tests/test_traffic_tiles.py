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
        clock[0] = 90
        assert (await service.get_flow_tile(*pasto_tile())).content == PNG
        assert len(calls) == 2
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_lru_uses_linked_list_and_evicts_least_recent_tile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.traffic.TRAFFIC_TILE_CACHE_MAX_ENTRIES", 2)
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    service = TrafficService(api_key="test", transport=httpx.MockTransport(respond))
    z, x, y = pasto_tile(18)
    try:
        await service.get_flow_tile(z, x, y)
        await service.get_flow_tile(z, x + 1, y)
        await service.get_flow_tile(z, x, y)
        await service.get_flow_tile(z, x + 2, y)
        await service.get_flow_tile(z, x + 1, y)
        assert len(calls) == 4
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_rate_limit_serves_marked_stale_data_only_for_bounded_time() -> None:
    clock = [0.0]
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
            if calls == 1
            else httpx.Response(429)
        )

    service = TrafficService(
        api_key="test", transport=httpx.MockTransport(respond), monotonic=lambda: clock[0]
    )
    try:
        first = await service.get_flow_tile(*pasto_tile())
        clock[0] = 90
        stale = await service.get_flow_tile(*pasto_tile())
        assert stale.cache_status == "STALE" and stale.observed_at == first.observed_at
        z, x, y = pasto_tile(18)
        with pytest.raises(TrafficRateLimitError):
            await service.get_flow_tile(z, x, y)
        assert calls == 2
        clock[0] = 601
        with pytest.raises(TrafficRateLimitError):
            await service.get_flow_tile(*pasto_tile())
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (429, TrafficRateLimitError),
        (500, TrafficProviderError),
        (404, TrafficNoCoverageError),
        (204, TrafficNoCoverageError),
    ],
)
async def test_http_failures_are_typed_without_exposing_key(
    status: int,
    expected: type[Exception],
) -> None:
    service = TrafficService(
        api_key="private-test-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status),
        ),
    )
    try:
        with pytest.raises(expected) as caught:
            await service.get_flow_tile(*pasto_tile())
        assert "private-test-key" not in str(caught.value)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_timeout_and_invalid_png_are_not_reported_as_clear_traffic() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network")

    for transport, expected in (
        (httpx.MockTransport(timeout), TrafficTimeoutError),
        (httpx.MockTransport(lambda _: httpx.Response(200, text="not PNG")), TrafficProviderError),
    ):
        service = TrafficService(api_key="test", transport=transport)
        try:
            with pytest.raises(expected):
                await service.get_flow_tile(*pasto_tile())
        finally:
            await service.aclose()
