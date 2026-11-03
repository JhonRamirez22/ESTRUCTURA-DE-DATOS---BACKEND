from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.traffic.tomtom import TrafficLevel, classify_traffic, parse_tomtom_flow_response


def _payload(
    *,
    current_speed: int = 14,
    free_flow_speed: int = 40,
    current_time: int = 180,
    free_flow_time: int = 65,
    confidence: float = 0.91,
    road_closure: bool = False,
) -> dict[str, object]:
    return {
        "flowSegmentData": {
            "currentSpeed": current_speed,
            "freeFlowSpeed": free_flow_speed,
            "currentTravelTime": current_time,
            "freeFlowTravelTime": free_flow_time,
            "confidence": confidence,
            "roadClosure": road_closure,
        }
    }


def test_tomtom_response_is_normalized_with_documented_units() -> None:
    observed_at = datetime(2026, 10, 3, 12, tzinfo=UTC)
    data = parse_tomtom_flow_response(_payload(), timestamp=observed_at)

    assert data.current_speed_kmh == 14
    assert data.free_flow_speed_kmh == 40
    assert data.current_travel_time_seconds == 180
    assert data.free_flow_travel_time_seconds == 65
    assert data.traffic_ratio == pytest.approx(0.35)
    assert data.travel_time_multiplier == pytest.approx(180 / 65)
    assert data.traffic_level == TrafficLevel.ALTO
    assert data.timestamp == observed_at


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.8, TrafficLevel.FLUIDO),
        (0.6, TrafficLevel.MODERADO),
        (0.35, TrafficLevel.ALTO),
        (0.34, TrafficLevel.MUY_ALTO),
        (None, TrafficLevel.DESCONOCIDO),
    ],
