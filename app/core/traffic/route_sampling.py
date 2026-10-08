"""Muestreo acotado de puntos sobre geometría vial ya calculada por OSRM/ORS."""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.core.routing.contracts import Coordinate, GeoJsonLineString

EARTH_RADIUS_METERS = 6_371_008.8
MAX_WAYPOINT_SNAP_METERS = 100
MAX_TRAFFIC_MAP_SAMPLES = 8
TRAFFIC_MAP_SAMPLE_SPACING_METERS = 900


def sample_route_leg_points(
    geometry: GeoJsonLineString,
    ordered_waypoints: Sequence[Coordinate],
) -> list[Coordinate]:
    """Devuelve un punto medio por tramo, proyectado sobre la polilínea vial y en su orden."""
    line = geometry["coordinates"]
    if len(ordered_waypoints) < 2 or len(line) < 2:
        return []

    cumulative = [0.0]
    for start, end in zip(line, line[1:], strict=False):
        cumulative.append(cumulative[-1] + _distance_meters(start, end))
    if cumulative[-1] <= 0:
        return []

    waypoint_distances: list[float] = []
    first = _project_waypoint(ordered_waypoints[0], line, cumulative, 0, 0)
    last = _project_waypoint(ordered_waypoints[-1], line, cumulative, len(line) - 2, len(line) - 2)
    if first is None or last is None:
        return []
    waypoint_distances.append(first[1])
    search_from = first[0]
    for waypoint in ordered_waypoints[1:-1]:
        projection = _project_waypoint(waypoint, line, cumulative, search_from)
        if projection is None:
            return []
        segment_index, along_distance = projection
        if along_distance <= waypoint_distances[-1]:
            return []
        waypoint_distances.append(along_distance)
        search_from = segment_index
    if last[1] <= waypoint_distances[-1]:
        return []
    waypoint_distances.append(last[1])

    return [
        _point_at_distance(line, cumulative, (start + end) / 2)
        for start, end in zip(waypoint_distances, waypoint_distances[1:], strict=False)
        if end - start > 1
    ]


def sample_route_geometry_points(
    geometry: GeoJsonLineString,
    *,
    max_samples: int = MAX_TRAFFIC_MAP_SAMPLES,
) -> list[Coordinate]:
    """Muestrea puntos sobre la calzada sin consultar cada vértice del trazado."""
    if max_samples <= 0:
        raise ValueError("max_samples debe ser positivo.")

    line = geometry["coordinates"]
    if len(line) < 2:
        return []

    cumulative = [0.0]
    for start, end in zip(line, line[1:], strict=False):
        cumulative.append(cumulative[-1] + _distance_meters(start, end))
    total_distance = cumulative[-1]
    if total_distance <= 0:
        return []

    # Una muestra por cada ~900 m permite ver congestión local sin convertir
    # el mapa en una consulta por segmento ni exceder el límite de TomTom.
    sample_count = min(
        max_samples,
        max(1, math.ceil(total_distance / TRAFFIC_MAP_SAMPLE_SPACING_METERS)),
    )
    return [
        _point_at_distance(line, cumulative, total_distance * (index + 0.5) / sample_count)
        for index in range(sample_count)
    ]


def _project_waypoint(
    waypoint: Coordinate,
    line: Sequence[Coordinate],
    cumulative: Sequence[float],
    min_segment_index: int,
    max_segment_index: int | None = None,
) -> tuple[int, float] | None:
    nearest_distance = math.inf
    nearest_projection: tuple[int, float] | None = None
    latitude_scale = math.pi * EARTH_RADIUS_METERS / 180
    longitude_scale = latitude_scale * math.cos(math.radians(waypoint[1]))
    point_x = waypoint[0] * longitude_scale
    point_y = waypoint[1] * latitude_scale

    end_index = len(line) - 2 if max_segment_index is None else max_segment_index
    for index in range(min_segment_index, end_index + 1):
        start, end = line[index], line[index + 1]
        start_x, start_y = start[0] * longitude_scale, start[1] * latitude_scale
        end_x, end_y = end[0] * longitude_scale, end[1] * latitude_scale
        direction_x, direction_y = end_x - start_x, end_y - start_y
        length_squared = direction_x**2 + direction_y**2
