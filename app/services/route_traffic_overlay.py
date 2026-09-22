"""Muestras acotadas de TomTom para visualizar congestión sobre una ruta."""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.core.routing.contracts import Coordinate, GeoJsonLineString
from app.core.traffic.route_sampling import sample_route_geometry_points
from app.core.traffic.tomtom import TrafficData

MIN_TRAFFIC_CONFIDENCE = 0.3


class TrafficOverlayProvider(Protocol):
    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData: ...


def traffic_observation_json(
    coordinate: Coordinate,
    data: TrafficData,
) -> dict[str, object]:
    """Mantiene el contrato del mapa pequeño y sin exponer datos personales."""
    return {
        "coordinates": [coordinate[0], coordinate[1]],
        "trafficLevel": data.traffic_level.value,
        "currentSpeedKmh": data.current_speed_kmh,
        "freeFlowSpeedKmh": data.free_flow_speed_kmh,
        "confidence": data.confidence,
        "roadClosure": data.road_closure,
        "observedAt": data.timestamp.isoformat(),
        "cacheStatus": data.cache_status,
    }


async def sample_route_traffic_overlay(
    geometry: GeoJsonLineString,
    provider: TrafficOverlayProvider | None,
) -> dict[str, object]:
    """Consulta como máximo ocho ubicaciones distribuidas por la geometría vial."""
    coordinates = sample_route_geometry_points(geometry)
    if provider is None:
        return {
            "source": "tomtom",
            "configured": False,
