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
