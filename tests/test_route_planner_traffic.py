from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.routing.contracts import Coordinate, GeoJsonLineString
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.services.route_planner import _apply_live_traffic_to_route


class FakeTrafficProvider:
    def __init__(self, response: TrafficData | Exception) -> None:
        self.response = response
        self.coordinates: list[Coordinate] = []

    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData:
        self.coordinates.append(coordinate)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _traffic(*, multiplier: float = 1.5, closed: bool = False) -> TrafficData:
    return TrafficData(
        current_speed_kmh=20,
        free_flow_speed_kmh=40,
        current_travel_time_seconds=90,
        free_flow_travel_time_seconds=60,
        confidence=0.9,
        road_closure=closed,
        traffic_ratio=0.5,
        traffic_level=TrafficLevel.CERRADO if closed else TrafficLevel.ALTO,
        travel_time_multiplier=multiplier,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_route_optimizer_scales_only_measured_osrm_legs() -> None:
    matrix: list[list[float | None]] = [
        [0, 4, 8],
        [5, 0, 6],
        [7, 9, 0],
    ]
    route = [0, 1, 2]
    coordinates: list[Coordinate] = [
        (-77.2811, 1.2136),
        (-77.2800, 1.2136),
        (-77.2790, 1.2140),
    ]
    geometry: GeoJsonLineString = {"type": "LineString", "coordinates": coordinates}
    provider = FakeTrafficProvider(_traffic(multiplier=1.5))

    adjusted, summary = await _apply_live_traffic_to_route(
        provider,
        matrix,
        route,
        coordinates,
        geometry,
    )

    assert adjusted[0][1] == 6
    assert adjusted[1][2] == 9
    assert adjusted[0][2] == 8
    assert len(provider.coordinates) == 2
    assert summary["applied"] is True
    assert summary["sampledSegments"] == 2
    assert summary["coverageRatio"] == 1


@pytest.mark.asyncio
async def test_closed_road_is_excluded_from_matrix_for_reoptimization() -> None:
    matrix: list[list[float | None]] = [[0, 4], [5, 0]]
    coordinates: list[Coordinate] = [(-77.2811, 1.2136), (-77.2800, 1.2136)]
    geometry: GeoJsonLineString = {"type": "LineString", "coordinates": coordinates}

    adjusted, summary = await _apply_live_traffic_to_route(
        FakeTrafficProvider(_traffic(closed=True)),
        matrix,
        [0, 1],
        coordinates,
        geometry,
    )

    assert adjusted[0][1] is None
    assert summary["closedSegments"] == 1
    assert summary["applied"] is True


@pytest.mark.asyncio
async def test_provider_failure_preserves_the_road_matrix() -> None:
    matrix: list[list[float | None]] = [[0, 4], [5, 0]]
    coordinates: list[Coordinate] = [(-77.2811, 1.2136), (-77.2800, 1.2136)]
    geometry: GeoJsonLineString = {"type": "LineString", "coordinates": coordinates}

    adjusted, summary = await _apply_live_traffic_to_route(
        FakeTrafficProvider(TimeoutError("simulated timeout")),
        matrix,
        [0, 1],
        coordinates,
        geometry,
    )

    assert adjusted == matrix
    assert summary["applied"] is False
    assert summary["errorSegments"] == 1
    warning = summary["warning"]
    assert isinstance(warning, str)
    assert "costo base" in warning
