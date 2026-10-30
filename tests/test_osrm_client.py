from __future__ import annotations

import httpx
import pytest

from app.core.routing.osrm_client import (
    OSRM_MAX_SNAP_DISTANCE_METERS,
    OsrmClient,
    OsrmClientError,
    OsrmRateLimitError,
    OsrmTimeoutError,
)

COORDINATES = [(-77.2811, 1.2136), (-77.28, 1.214)]
ROAD_GEOMETRY = [
    (-77.2811, 1.2136),
    (-77.2808, 1.2137),
    (-77.2805, 1.2138),
    (-77.2802, 1.2139),
    (-77.28, 1.214),
]


@pytest.mark.asyncio
async def test_osrm_normalizes_seconds_to_minutes_and_requests_road_geometry() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/table/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "durations": [[0, 300], [360, 0]],
                    "distances": [[0, 1_500], [1_800, 0]],
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "waypoints": [
                    {"location": list(location), "distance": 4} for location in COORDINATES
                ],
                "routes": [
                    {
                        "distance": 2_400,
                        "duration": 600,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [list(point) for point in ROAD_GEOMETRY],
                        },
                    }
                ],
            },
        )

    async with OsrmClient(transport=httpx.MockTransport(handler)) as client:
        matrix = await client.get_duration_matrix(COORDINATES)
        route = await client.get_route(COORDINATES)

    assert matrix == {
        "durations_minutes": [[0, 5], [6, 0]],
        "distances_meters": [[0, 1_500], [1_800, 0]],
    }
    assert route == {
        "geometry": {"type": "LineString", "coordinates": ROAD_GEOMETRY},
        "geometry_provider": "OSRM",
        "distance_meters": 2_400,
        "duration_minutes": 10,
        "snap_distances_meters": [4, 4],
    }
    assert requests[0].url.params["radiuses"] == f"{OSRM_MAX_SNAP_DISTANCE_METERS};50"
    assert requests[1].url.params["geometries"] == "geojson"
