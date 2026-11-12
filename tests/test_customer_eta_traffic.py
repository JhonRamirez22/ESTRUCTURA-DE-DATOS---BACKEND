from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.tomtom import TrafficCacheStatus, TrafficData, TrafficLevel
from app.services.customer_eta_traffic import (
    clear_customer_eta_traffic_cache,
    estimate_first_leg_with_traffic,
)


class _RouteProvider:
    async def get_route(self, coordinates: list[Coordinate]) -> RouteResult:
        return {
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "geometry_provider": "OSRM",
            "distance_meters": 1_200,
            "duration_minutes": 8,
        }


class _TrafficProvider:
    def __init__(self, data: TrafficData | Exception) -> None:
        self.data = data
        self.coordinates: list[Coordinate] = []

    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData:
        self.coordinates.append(coordinate)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def _traffic(
    *,
    multiplier: float = 1.5,
    confidence: float = 0.9,
    road_closure: bool = False,
    cache_status: TrafficCacheStatus = "LIVE",
) -> TrafficData:
    return TrafficData(
        current_speed_kmh=20,
        free_flow_speed_kmh=40,
        current_travel_time_seconds=90,
        free_flow_travel_time_seconds=60,
        confidence=confidence,
        road_closure=road_closure,
        traffic_ratio=0.5,
        traffic_level=TrafficLevel.CERRADO if road_closure else TrafficLevel.ALTO,
        travel_time_multiplier=multiplier,
        timestamp=datetime.now(UTC),
        cache_status=cache_status,
    )


@pytest.fixture(autouse=True)
def clear_traffic_cache() -> None:
    clear_customer_eta_traffic_cache()


@pytest.mark.asyncio
async def test_applies_live_traffic_to_verified_next_leg_and_caches_by_route() -> None:
    route_id, stop_id = uuid4(), uuid4()
    provider = _TrafficProvider(_traffic())
    route = _RouteProvider()
    arguments = {
        "route_id": route_id,
        "next_stop_id": stop_id,
        "start": (-77.2811, 1.2136),
        "end": (-77.279, 1.215),
        "base_duration_minutes": 11,
        "base_distance_meters": 1_000,
        "route_provider": route,
        "traffic_provider": provider,
    }

    first = await estimate_first_leg_with_traffic(**arguments)
    second = await estimate_first_leg_with_traffic(**arguments)

    assert first.duration_minutes == 12
    assert first.distance_meters == 1_200
    assert first.applied and first.status == "LIVE"
    assert second.applied and second.status == "CACHE"
    assert len(provider.coordinates) == 1


@pytest.mark.asyncio
async def test_low_confidence_traffic_keeps_the_verified_road_time() -> None:
    estimate = await estimate_first_leg_with_traffic(
        route_id=uuid4(),
        next_stop_id=uuid4(),
        start=(-77.2811, 1.2136),
        end=(-77.279, 1.215),
        base_duration_minutes=11,
        base_distance_meters=1_000,
        route_provider=_RouteProvider(),
        traffic_provider=_TrafficProvider(_traffic(confidence=0.2)),
    )

    assert estimate.duration_minutes == 8
    assert not estimate.applied
    assert "factor confiable" in (estimate.warning or "")


@pytest.mark.asyncio
async def test_reported_closure_removes_the_eta_for_the_next_leg() -> None:
    estimate = await estimate_first_leg_with_traffic(
        route_id=uuid4(),
        next_stop_id=uuid4(),
        start=(-77.2811, 1.2136),
        end=(-77.279, 1.215),
        base_duration_minutes=11,
        base_distance_meters=1_000,
        route_provider=_RouteProvider(),
        traffic_provider=_TrafficProvider(_traffic(road_closure=True)),
    )

    assert estimate.duration_minutes is None
    assert estimate.applied and estimate.closed
    assert "posible cierre" in (estimate.warning or "")


@pytest.mark.asyncio
async def test_tomtom_error_keeps_the_base_time_and_explicit_warning() -> None:
    estimate = await estimate_first_leg_with_traffic(
        route_id=uuid4(),
        next_stop_id=uuid4(),
        start=(-77.2811, 1.2136),
        end=(-77.279, 1.215),
        base_duration_minutes=11,
        base_distance_meters=1_000,
        route_provider=_RouteProvider(),
        traffic_provider=_TrafficProvider(TimeoutError("provider secret")),
    )

    assert estimate.duration_minutes == 8
    assert not estimate.applied
    assert "no respondió" in (estimate.warning or "")
    assert "provider secret" not in (estimate.warning or "")
