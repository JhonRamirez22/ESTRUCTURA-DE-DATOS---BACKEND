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
            "available": False,
            "requestedSegments": 0,
            "sampledSegments": 0,
            "observations": [],
            "warning": "TomTom Traffic no está configurado.",
        }

    responses = await asyncio.gather(
        *(provider.get_flow_segment(coordinate) for coordinate in coordinates),
        return_exceptions=True,
    )
    observations: list[dict[str, object]] = []
    error_count = 0
    low_confidence_count = 0
    for coordinate, response in zip(coordinates, responses, strict=True):
        if isinstance(response, BaseException):
            if isinstance(response, asyncio.CancelledError):
                raise response
            if not isinstance(response, Exception):
                raise response
            error_count += 1
            continue
        if response.confidence < MIN_TRAFFIC_CONFIDENCE and not response.road_closure:
            low_confidence_count += 1
            continue
        observations.append(traffic_observation_json(coordinate, response))

    warning_parts = ["TomTom muestra puntos muestreados de esta ruta, no cobertura de toda Pasto."]
    if error_count:
        warning_parts.append(f"{error_count} muestras no respondieron.")
    if low_confidence_count:
        warning_parts.append(f"Se omitieron {low_confidence_count} muestras de baja confianza.")
    if not observations:
        warning_parts.append("No hay observaciones confiables para mostrar ahora.")

    return {
        "source": "tomtom",
        "configured": True,
        "available": bool(observations),
        "requestedSegments": len(coordinates),
        "sampledSegments": len(observations),
        "observations": observations,
        "warning": " ".join(warning_parts),
    }
