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
