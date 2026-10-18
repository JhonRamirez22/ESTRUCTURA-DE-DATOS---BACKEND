from __future__ import annotations

import pytest

from app.services.route_planner import RouteConflictError, _read_route_revision_snapshot


def test_persisted_route_snapshot_restores_ordered_stops_and_metrics() -> None:
    snapshot = _read_route_revision_snapshot(
        {
            "version": 1,
            "estimatedDurationMinutes": 18.5,
            "estimatedDistanceMeters": 2_300,
            "baselineDurationMinutes": 20,
            "baselineDistanceMeters": 2_700,
            "geometry": {"type": "LineString", "coordinates": [[-77.28, 1.21]]},
            "geometryProvider": "OSRM",
            "activeStops": [
                {"deliveryPointId": "00000000-0000-0000-0000-000000000001", "sequenceIndex": 0},
                {"deliveryPointId": "00000000-0000-0000-0000-000000000002", "sequenceIndex": 1},
            ],
        }
    )

    assert snapshot.estimated_duration_minutes == 18.5
    assert snapshot.estimated_distance_meters == 2_300
    assert snapshot.geometry_provider.value == "OSRM"
    assert [index for _point_id, index in snapshot.active_stops] == [0, 1]


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {"version": 2},
        {
            "version": 1,
            "estimatedDurationMinutes": -1,
            "estimatedDistanceMeters": 10,
            "activeStops": [],
        },
        {
            "version": 1,
            "estimatedDurationMinutes": 5,
            "estimatedDistanceMeters": 10,
            "activeStops": [
                {"deliveryPointId": "00000000-0000-0000-0000-000000000001", "sequenceIndex": 0},
                {"deliveryPointId": "00000000-0000-0000-0000-000000000001", "sequenceIndex": 1},
            ],
        },
    ],
)
def test_invalid_persisted_route_snapshot_is_rejected(snapshot: object) -> None:
    with pytest.raises(RouteConflictError):
        _read_route_revision_snapshot(snapshot)
