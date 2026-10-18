from __future__ import annotations

from app.core.routing.contracts import Coordinate
from app.core.traffic.route_sampling import sample_route_leg_points


def test_samples_one_point_on_each_ordered_road_leg() -> None:
    geometry = {
        "type": "LineString",
        "coordinates": [
            (-77.2811, 1.2136),
            (-77.2800, 1.2136),
            (-77.2790, 1.2140),
            (-77.2780, 1.2140),
        ],
    }
    waypoints: list[Coordinate] = [
        (-77.2811, 1.2136),
        (-77.2790, 1.2140),
        (-77.2780, 1.2140),
    ]

    samples = sample_route_leg_points(geometry, waypoints)

    assert len(samples) == 2
    assert samples[0][0] < -77.2790
    assert samples[1][0] < -77.2780


def test_does_not_sample_if_waypoint_is_not_on_verified_road_geometry() -> None:
    geometry = {
        "type": "LineString",
        "coordinates": [(-77.2811, 1.2136), (-77.2800, 1.2136)],
    }
    waypoints: list[Coordinate] = [
        (-77.2811, 1.2136),
        (-77.25, 1.25),
    ]

    assert sample_route_leg_points(geometry, waypoints) == []
