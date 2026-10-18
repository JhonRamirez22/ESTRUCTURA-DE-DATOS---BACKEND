from __future__ import annotations

import httpx
import pytest

from app.core.routing.contracts import Coordinate
from app.core.routing.ors_client import (
    OrsClient,
    OrsClientError,
    OrsRateLimitError,
    OrsTimeoutError,
)

COORDINATES: list[Coordinate] = [(-77.2811, 1.2136), (-77.28, 1.214)]


@pytest.mark.asyncio
async def test_ors_normalizes_seconds_and_sends_key_without_external_network() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/matrix/" in request.url.path:
            return httpx.Response(200, json={"durations": [[0, 600], [720, 0]]})
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [list(point) for point in COORDINATES],
                        },
                        "properties": {"summary": {"distance": 2_400, "duration": 900}},
                    }
                ]
            },
        )

    async with OrsClient(
        api_key="test-api-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        matrix = await client.get_duration_matrix(COORDINATES)
        route = await client.get_route(COORDINATES)

    assert matrix == {"durations_minutes": [[0, 10], [12, 0]]}
    assert route == {
        "geometry": {"type": "LineString", "coordinates": COORDINATES},
        "geometry_provider": "ORS",
        "distance_meters": 2_400,
        "duration_minutes": 15,
    }
    assert requests[0].headers["Authorization"] == "test-api-key"
    assert (
        requests[0].read()
        == b'{"locations":[[-77.2811,1.2136],[-77.28,1.214]],"metrics":["duration"]}'
    )


def test_ors_requires_an_api_key_when_constructed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ORS_API_KEY"):
        OrsClient()


@pytest.mark.asyncio
async def test_ors_distinguishes_rate_limit_and_http_errors_with_body() -> None:
    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(429, text="too many")),
    ) as client:
        with pytest.raises(OrsRateLimitError, match="too many") as captured:
            await client.get_duration_matrix(COORDINATES)
    assert captured.value.status == 429
    assert captured.value.response_body == "too many"

    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="service unavailable")
        ),
    ) as client:
        with pytest.raises(OrsClientError, match="HTTP 500.*service unavailable") as captured:
            await client.get_route(COORDINATES)
    assert captured.value.status == 500
    assert captured.value.response_body == "service unavailable"


@pytest.mark.asyncio
async def test_ors_converts_http_timeout_to_typed_error() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request took too long", request=request)

    async with OrsClient(
        api_key="test-key",
        timeout_seconds=0.02,
        transport=httpx.MockTransport(timeout_handler),
    ) as client:
        with pytest.raises(OrsTimeoutError, match="20 ms"):
            await client.get_duration_matrix(COORDINATES)


@pytest.mark.asyncio
async def test_ors_rejects_malformed_matrix_geometry_and_metrics() -> None:
    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"durations": [[0, 60]]})
        ),
    ) as client:
        with pytest.raises(OrsClientError, match="incorrect dimensions"):
            await client.get_duration_matrix(COORDINATES)

    malformed_route = {
        "features": [
            {
                "geometry": {"type": "LineString", "coordinates": [[-77.28, 1.21]]},
                "properties": {"summary": {"distance": 10, "duration": 10}},
            }
        ]
    }
    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=malformed_route)),
    ) as client:
        with pytest.raises(OrsClientError, match="invalid geometry"):
            await client.get_route(COORDINATES)


@pytest.mark.asyncio
async def test_ors_rejects_negative_routing_costs() -> None:
    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"durations": [[0, -1], [60, 0]]})
        ),
    ) as client:
        with pytest.raises(OrsClientError, match="invalid duration"):
            await client.get_duration_matrix(COORDINATES)

    negative_route = {
        "features": [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(point) for point in COORDINATES],
                },
                "properties": {"summary": {"distance": 10, "duration": -1}},
            }
        ]
    }
    async with OrsClient(
        api_key="test-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=negative_route)),
    ) as client:
        with pytest.raises(OrsClientError, match="invalid duration"):
            await client.get_route(COORDINATES)
