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
