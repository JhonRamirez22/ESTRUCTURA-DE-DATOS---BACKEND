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
