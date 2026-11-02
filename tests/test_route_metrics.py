from __future__ import annotations

import pytest

from app.core.metrics.route_metrics import (
    RouteMetricRecord,
    calculate_fleet_metrics,
    calculate_route_efficiency,
)


def test_route_efficiency_calculates_savings_and_handles_zero_baseline() -> None:
    metrics = calculate_route_efficiency(
        RouteMetricRecord("COMPLETED", 20, 15, 5_000, 4_000)
    )
    zero_baseline = calculate_route_efficiency(
        RouteMetricRecord("PLANNED", 0, 4, 0, 500)
    )

    assert metrics.time_saved_minutes == 5
    assert metrics.time_saved_percent == 25
    assert metrics.distance_saved_meters == 1_000
    assert metrics.distance_saved_percent == 20
    assert zero_baseline.time_saved_percent == 0
    assert zero_baseline.distance_saved_percent == 0
