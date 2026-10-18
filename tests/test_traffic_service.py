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
        calls += 1
        return httpx.Response(200, json=_response(speed=10 + calls))

    service = TrafficService(
        api_key="test-only-key",
        transport=httpx.MockTransport(respond),
        monotonic=lambda: clock[0],
    )
    try:
        first = await service.get_flow_segment((-77.2811, 1.2136))
        clock[0] += TRAFFIC_CACHE_TTL_SECONDS + 1
        refreshed = await service.get_flow_segment((-77.2811, 1.2136))
    finally:
        await service.aclose()

    assert first.current_speed_kmh == 11
    assert refreshed.current_speed_kmh == 12
    assert refreshed.cache_status == "LIVE"
    assert calls == 2


@pytest.mark.asyncio
async def test_timeout_uses_recent_stale_cache_without_retries() -> None:
    clock = [20.0]
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_response())
        raise httpx.ReadTimeout("simulated timeout", request=request)

    service = TrafficService(
        api_key="test-only-key",
        transport=httpx.MockTransport(respond),
        monotonic=lambda: clock[0],
    )
    try:
        await service.get_flow_segment((-77.2811, 1.2136))
        clock[0] += TRAFFIC_CACHE_TTL_SECONDS + 1
        stale = await service.get_flow_segment((-77.2811, 1.2136))
    finally:
        await service.aclose()

    assert stale.cache_status == "STALE"
    assert stale.current_speed_kmh == 20
    assert calls == 2


@pytest.mark.asyncio
async def test_http_429_is_typed_and_cooldown_prevents_immediate_retry() -> None:
    clock = [30.0]
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "rate limited"})

    service = TrafficService(
        api_key="test-only-key",
        transport=httpx.MockTransport(respond),
        monotonic=lambda: clock[0],
    )
    try:
        with pytest.raises(TrafficRateLimitError):
            await service.get_flow_segment((-77.2811, 1.2136))
        with pytest.raises(TrafficRateLimitError):
            await service.get_flow_segment((-77.2811, 1.2136))
    finally:
        await service.aclose()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 500, 503])
async def test_provider_http_errors_are_controlled(status_code: int) -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="provider error")

    service = TrafficService(
        api_key="test-only-key",
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(TrafficProviderError) as error:
            await service.get_flow_segment((-77.2811, 1.2136))
    finally:
        await service.aclose()

    assert error.value.status_code == status_code
    assert "test-only-key" not in str(error.value)


@pytest.mark.asyncio
async def test_invalid_json_is_controlled_and_outside_pasto_is_rejected_before_network() -> None:
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"unexpected": True})

    service = TrafficService(api_key="test-only-key", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(TrafficProviderError, match="respuesta inválida"):
            await service.get_flow_segment((-77.2811, 1.2136))
        with pytest.raises(ValueError, match="zona de servicio de Pasto"):
            await service.get_flow_segment((-70.0, 4.0))
    finally:
        await service.aclose()

    assert calls == 1


def test_traffic_cache_key_rounds_gps_jitter_to_five_decimals() -> None:
    assert build_traffic_cache_key((-77.2811001, 1.2136001)) == build_traffic_cache_key(
        (-77.2811002, 1.2136002)
    )


def test_traffic_service_rejects_missing_key() -> None:
    with pytest.raises(ValueError, match="TOMTOM_API_KEY"):
        TrafficService(api_key=" ")
