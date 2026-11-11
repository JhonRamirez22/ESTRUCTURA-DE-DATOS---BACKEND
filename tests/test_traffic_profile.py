from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.routing.contracts import GeoJsonLineString
from app.core.traffic.congestion_factors import (
    TravelTimeSample,
    build_congestion_factor_key,
    calculate_congestion_factors,
    get_congestion_factor,
)
from app.core.traffic.map_matching import (
    LocationEventPoint,
    match_location_events_to_segments,
)
from app.core.traffic.road_segments import derive_road_segments


def test_road_segments_preserve_total_duration_and_map_matching_ignores_distant_gps() -> None:
    geometry: GeoJsonLineString = {
        "type": "LineString",
        "coordinates": [(-77.2811, 1.2136), (-77.2805, 1.2139), (-77.28, 1.214)],
    }
    segments = derive_road_segments(geometry, 12)
    assert len(segments) == 2
    assert sum(segment.ors_duration_minutes for segment in segments) == pytest.approx(12)

    start = datetime(2026, 9, 25, 12, tzinfo=UTC)
    events = [
        LocationEventPoint((-77.2811, 1.2136), start),
        LocationEventPoint((-77.2809, 1.2137), start + timedelta(minutes=1)),
        LocationEventPoint((-77.2, 1.3), start + timedelta(minutes=2)),
    ]
    samples = match_location_events_to_segments(events, segments)
    assert len(samples) == 1
    assert samples[0].observed_duration_minutes == pytest.approx(1)
    assert samples[0].hour_of_day == 7


def test_congestion_factors_require_repeated_samples_and_use_clamped_median() -> None:
    samples = [
        TravelTimeSample("segment", 8, 2, 10),
        TravelTimeSample("segment", 8, 2, 12),
        TravelTimeSample("segment", 8, 2, 14),
        TravelTimeSample("other", 8, 2, 5),
    ]
    factors = calculate_congestion_factors(samples)
    assert factors["segment:8"] == 3
    assert factors["other:8"] == 1
    assert get_congestion_factor(factors, "missing", 8) == 1


def test_traffic_contract_rejects_invalid_segments_and_hour_keys() -> None:
    geometry: GeoJsonLineString = {"type": "LineString", "coordinates": []}
    with pytest.raises(ValueError, match="positivo"):
        derive_road_segments(geometry, 0)
    assert calculate_congestion_factors([TravelTimeSample("x", 24, 1, 2)]) == {}
    with pytest.raises(ValueError, match="entre 0 y 23"):
        build_congestion_factor_key("x", 24)
