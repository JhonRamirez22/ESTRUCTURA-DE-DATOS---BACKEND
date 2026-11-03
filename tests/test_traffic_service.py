from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.services.traffic import (
    TRAFFIC_CACHE_TTL_SECONDS,
    TrafficProviderError,
    TrafficRateLimitError,
    TrafficService,
    build_traffic_cache_key,
)


def _response(*, speed: int = 20) -> dict[str, object]:
    return {
        "flowSegmentData": {
            "currentSpeed": speed,
            "freeFlowSpeed": 40,
            "currentTravelTime": 120,
            "freeFlowTravelTime": 60,
            "confidence": 0.9,
            "roadClosure": False,
        }
    }


@pytest.mark.asyncio
async def test_traffic_service_sends_latitude_longitude_and_caches_success() -> None:
    calls: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_response())

    service = TrafficService(
        api_key="test-only-key",
        transport=httpx.MockTransport(respond),
        utc_now=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )
    try:
        live = await service.get_flow_segment((-77.2811, 1.2136))
        cached = await service.get_flow_segment((-77.2811001, 1.2136001))
    finally:
        await service.aclose()

    assert len(calls) == 1
    assert calls[0].url.params["point"] == "1.21360,-77.28110"
    assert calls[0].url.params["unit"] == "kmph"
    assert calls[0].url.params["key"] == "test-only-key"
    assert live.cache_status == "LIVE"
    assert cached.cache_status == "CACHE"
    assert cached.current_speed_kmh == 20


@pytest.mark.asyncio
async def test_expired_cache_refreshes_after_ttl() -> None:
    clock = [10.0]
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
