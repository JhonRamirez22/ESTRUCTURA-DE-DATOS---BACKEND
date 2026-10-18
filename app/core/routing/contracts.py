"""Contratos compartidos de coordenadas, matrices y geometrías viales."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal, NotRequired, TypedDict

Coordinate = tuple[float, float]
type GeometryProvider = Literal["OSRM", "ORS"]


class GeoJsonLineString(TypedDict):
    type: Literal["LineString"]
    coordinates: list[Coordinate]


class MatrixResult(TypedDict):
    durations_minutes: list[list[float | None]]
    distances_meters: NotRequired[list[list[float | None]]]


class RouteResult(TypedDict):
    geometry: GeoJsonLineString
    geometry_provider: GeometryProvider
    distance_meters: float
    duration_minutes: float
    snap_distances_meters: NotRequired[list[float]]


def validate_coordinates(coordinates: Sequence[Coordinate]) -> None:
    if len(coordinates) < 2:
        raise ValueError("Se requieren al menos dos coordenadas para calcular una ruta")
    for coordinate in coordinates:
        if (
            len(coordinate) != 2
            or not math.isfinite(coordinate[0])
            or not -180 <= coordinate[0] <= 180
            or not math.isfinite(coordinate[1])
            or not -90 <= coordinate[1] <= 90
        ):
            raise ValueError("Las coordenadas deben ser [lng, lat] válidas")
