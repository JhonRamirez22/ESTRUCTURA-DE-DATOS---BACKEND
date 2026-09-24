"""Comparación determinista entre costo base y costo optimizado de rutas."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouteMetricRecord:
    status: str
    baseline_duration_minutes: float | None
    estimated_duration_minutes: float
    baseline_distance_meters: float | None
    estimated_distance_meters: float


@dataclass(frozen=True, slots=True)
class RouteEfficiency:
    time_saved_minutes: float | None
    time_saved_percent: float | None
    distance_saved_meters: float | None
    distance_saved_percent: float | None


@dataclass(frozen=True, slots=True)
class FleetMetrics:
    route_count: int
    completed_route_count: int
    estimated_duration_minutes: float
    estimated_distance_meters: float
    time_saved_minutes: float
    distance_saved_meters: float
    routes_with_baseline: int


def calculate_route_efficiency(input_record: RouteMetricRecord) -> RouteEfficiency:
    _validate_non_negative(input_record.estimated_duration_minutes, "estimatedDurationMinutes")
    _validate_non_negative(input_record.estimated_distance_meters, "estimatedDistanceMeters")
    time_saved = _calculate_savings(
        input_record.baseline_duration_minutes, input_record.estimated_duration_minutes
    )
    distance_saved = _calculate_savings(
        input_record.baseline_distance_meters, input_record.estimated_distance_meters
    )
    return RouteEfficiency(
        time_saved_minutes=time_saved,
        time_saved_percent=_calculate_savings_percent(
            input_record.baseline_duration_minutes, input_record.estimated_duration_minutes
        ),
        distance_saved_meters=distance_saved,
        distance_saved_percent=_calculate_savings_percent(
            input_record.baseline_distance_meters, input_record.estimated_distance_meters
