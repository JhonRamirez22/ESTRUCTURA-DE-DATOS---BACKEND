"""Deriva segmentos estables de la geometría vial guardada en una ruta."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.routing.contracts import Coordinate, GeoJsonLineString

_METERS_PER_DEGREE_LATITUDE = 111_320


@dataclass(frozen=True, slots=True)
class RoadSegment:
    id: str
    start: Coordinate
    end: Coordinate
    ors_duration_minutes: float


def derive_road_segments(
    geometry: GeoJsonLineString,
    total_duration_minutes: float,
) -> list[RoadSegment]:
    if not math.isfinite(total_duration_minutes) or total_duration_minutes <= 0:
        raise ValueError("total_duration_minutes debe ser positivo y finito.")
    coordinates = geometry["coordinates"]
    if geometry["type"] != "LineString" or len(coordinates) < 2:
        return []
    lengths = [
        _distance_meters(start, end)
        for start, end in zip(coordinates, coordinates[1:], strict=False)
    ]
    total_length = sum(lengths)
    if total_length == 0:
        return []
    return [
        RoadSegment(
            id=f"geo:{_coordinate_key(start)}->{_coordinate_key(end)}",
            start=start,
            end=end,
            ors_duration_minutes=total_duration_minutes * (length / total_length),
        )
        for (start, end), length in zip(
            zip(coordinates, coordinates[1:], strict=False),
            lengths,
            strict=True,
        )
    ]


def _coordinate_key(coordinate: Coordinate) -> str:
    return f"{coordinate[0]:.5f},{coordinate[1]:.5f}"


def _distance_meters(start: Coordinate, end: Coordinate) -> float:
    mean_latitude = math.radians((start[1] + end[1]) / 2)
    delta_x = (end[0] - start[0]) * _METERS_PER_DEGREE_LATITUDE * math.cos(mean_latitude)
    delta_y = (end[1] - start[1]) * _METERS_PER_DEGREE_LATITUDE
    return math.hypot(delta_x, delta_y)
