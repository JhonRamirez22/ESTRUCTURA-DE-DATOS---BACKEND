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
