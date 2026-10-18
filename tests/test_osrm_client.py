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


@pytest.mark.asyncio
async def test_osrm_distinguishes_rate_limit_and_http_errors_with_body() -> None:
    async with OsrmClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(429, text="too many"))
    ) as client:
        with pytest.raises(OsrmRateLimitError, match="too many"):
            await client.get_route(COORDINATES)

    async with OsrmClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="upstream"))
    ) as client:
        with pytest.raises(OsrmClientError, match="HTTP 500.*upstream") as captured:
            await client.get_route(COORDINATES)
    assert captured.value.status == 500
    assert captured.value.response_body == "upstream"


@pytest.mark.asyncio
async def test_osrm_converts_http_timeout_to_typed_error() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request took too long", request=request)

    async with OsrmClient(
        timeout_seconds=0.01,
        transport=httpx.MockTransport(timeout_handler),
    ) as client:
        with pytest.raises(OsrmTimeoutError, match="10 ms"):
            await client.get_route(COORDINATES)


@pytest.mark.asyncio
async def test_osrm_rejects_unsnapped_or_skipped_waypoints() -> None:
    too_far = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": "Ok",
                "waypoints": [
                    {"location": list(COORDINATES[0]), "distance": 4},
                    {
                        "location": list(COORDINATES[1]),
                        "distance": OSRM_MAX_SNAP_DISTANCE_METERS + 1,
                    },
                ],
                "routes": [
                    {
                        "distance": 100,
                        "duration": 60,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                list(COORDINATES[0]),
                                list(COORDINATES[1]),
                            ],
                        },
                    }
                ],
            },
        )
    )
    async with OsrmClient(transport=too_far) as client:
        with pytest.raises(OsrmClientError, match="within 50 m"):
            await client.get_route(COORDINATES)

    skipped_stop_coordinates = [
        (-77.2811, 1.2136),
        (-77.2811, 1.2146),
        (-77.28, 1.2146),
    ]
    skipped_stop = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": "Ok",
                "waypoints": [
                    {"location": list(point), "distance": 0} for point in skipped_stop_coordinates
                ],
                "routes": [
                    {
                        "distance": 200,
                        "duration": 60,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                list(skipped_stop_coordinates[0]),
                                list(skipped_stop_coordinates[2]),
                            ],
                        },
                    }
                ],
            },
        )
    )
    async with OsrmClient(transport=skipped_stop) as client:
        with pytest.raises(OsrmClientError, match="does not pass through waypoint 2"):
            await client.get_route(skipped_stop_coordinates)


@pytest.mark.asyncio
async def test_osrm_rejects_invalid_coordinates_without_network() -> None:
    async with OsrmClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="al menos dos"):
            await client.get_route([(-77.28, 1.21)])


@pytest.mark.asyncio
async def test_osrm_rejects_negative_routing_costs() -> None:
    negative_matrix = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": "Ok",
                "durations": [[0, -1], [60, 0]],
                "distances": [[0, 10], [10, 0]],
            },
        )
    )
    async with OsrmClient(transport=negative_matrix) as client:
        with pytest.raises(OsrmClientError, match="invalid durations value"):
            await client.get_duration_matrix(COORDINATES)

    negative_route = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": "Ok",
                "waypoints": [
                    {"location": list(location), "distance": 0} for location in COORDINATES
                ],
                "routes": [
                    {
                        "distance": 10,
                        "duration": -1,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [list(point) for point in ROAD_GEOMETRY],
                        },
                    }
                ],
            },
        )
    )
    async with OsrmClient(transport=negative_route) as client:
        with pytest.raises(OsrmClientError, match="invalid route duration"):
            await client.get_route(COORDINATES)
