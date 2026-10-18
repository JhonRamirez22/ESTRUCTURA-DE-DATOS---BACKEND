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


def test_fleet_metrics_omits_routes_without_a_complete_baseline() -> None:
    metrics = calculate_fleet_metrics(
        [
            RouteMetricRecord("COMPLETED", 20, 15, 5_000, 4_000),
            RouteMetricRecord("PLANNED", None, 10, None, 1_500),
            RouteMetricRecord("CANCELLED", 12, 8, 2_000, 1_000),
        ]
    )

    assert metrics.route_count == 3
    assert metrics.completed_route_count == 1
    assert metrics.estimated_duration_minutes == 33
    assert metrics.estimated_distance_meters == 6_500
    assert metrics.time_saved_minutes == 9
    assert metrics.distance_saved_meters == 2_000
    assert metrics.routes_with_baseline == 2


def test_empty_fleet_has_zero_totals_and_invalid_metrics_are_rejected() -> None:
    assert calculate_fleet_metrics([]).route_count == 0
    with pytest.raises(ValueError, match="finite and non-negative"):
        calculate_route_efficiency(RouteMetricRecord("PLANNED", None, -1, None, 0))

