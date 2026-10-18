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
        ),
    )


def calculate_fleet_metrics(routes: Iterable[RouteMetricRecord]) -> FleetMetrics:
    records = list(routes)
    duration = 0.0
    distance = 0.0
    saved_duration = 0.0
    saved_distance = 0.0
    routes_with_baseline = 0

    for route in records:
        efficiency = calculate_route_efficiency(route)
        duration += route.estimated_duration_minutes
        distance += route.estimated_distance_meters
        saved_duration += efficiency.time_saved_minutes or 0.0
        saved_distance += efficiency.distance_saved_meters or 0.0
        has_complete_baseline = (
            efficiency.time_saved_minutes is not None
            and efficiency.distance_saved_meters is not None
        )
        if has_complete_baseline:
            routes_with_baseline += 1

    return FleetMetrics(
        route_count=len(records),
        completed_route_count=sum(route.status == "COMPLETED" for route in records),
        estimated_duration_minutes=duration,
        estimated_distance_meters=distance,
        time_saved_minutes=saved_duration,
        distance_saved_meters=saved_distance,
        routes_with_baseline=routes_with_baseline,
    )


def _calculate_savings(baseline: float | None, optimized: float) -> float | None:
    if baseline is None:
        return None
    _validate_non_negative(baseline, "baseline")
    return baseline - optimized


def _calculate_savings_percent(baseline: float | None, optimized: float) -> float | None:
    if baseline is None:
        return None
    _validate_non_negative(baseline, "baseline")
    return 0.0 if baseline == 0 else ((baseline - optimized) / baseline) * 100


def _validate_non_negative(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
