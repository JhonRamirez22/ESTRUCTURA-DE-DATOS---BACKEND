"""Empareja eventos GPS cercanos con tramos y forma muestras observadas."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.routing.contracts import Coordinate
from app.core.traffic.congestion_factors import TravelTimeSample
from app.core.traffic.road_segments import RoadSegment

DEFAULT_MAX_MATCH_DISTANCE_DEGREES = 0.002
_PASTO_TIMEZONE = ZoneInfo("America/Bogota")


@dataclass(frozen=True, slots=True)
class LocationEventPoint:
    coordinates: Coordinate
    recorded_at: datetime


def match_location_events_to_segments(
    events: Sequence[LocationEventPoint],
    segments: Sequence[RoadSegment],
    *,
    max_match_distance_degrees: float = DEFAULT_MAX_MATCH_DISTANCE_DEGREES,
) -> list[TravelTimeSample]:
    if not math.isfinite(max_match_distance_degrees) or max_match_distance_degrees <= 0:
        raise ValueError("max_match_distance_degrees debe ser positivo y finito.")
    valid_segments = [segment for segment in segments if _valid_segment(segment)]
    ordered_events = sorted(
        (event for event in events if _valid_event(event)),
        key=lambda event: _as_utc(event.recorded_at),
    )
    if len(ordered_events) < 2 or not valid_segments:
        return []

    matches = [
        (
            event,
            _find_nearest_segment(event.coordinates, valid_segments, max_match_distance_degrees),
        )
        for event in ordered_events
    ]
    samples: list[TravelTimeSample] = []
    run_segment: RoadSegment | None = None
    run_start: LocationEventPoint | None = None
    run_end: LocationEventPoint | None = None
