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
    run_event_count = 0

    def flush_run() -> None:
        if run_segment is None or run_start is None or run_end is None or run_event_count < 2:
            return
        elapsed_seconds = (
            _as_utc(run_end.recorded_at) - _as_utc(run_start.recorded_at)
        ).total_seconds()
        observed_minutes = elapsed_seconds / 60
        if observed_minutes > 0:
            samples.append(
                TravelTimeSample(
                    segment_id=run_segment.id,
                    hour_of_day=_as_utc(run_start.recorded_at).astimezone(_PASTO_TIMEZONE).hour,
                    ors_duration_minutes=run_segment.ors_duration_minutes,
                    observed_duration_minutes=observed_minutes,
                )
            )

    for event, segment in matches:
        if segment is None or segment.id != (run_segment.id if run_segment else None):
            flush_run()
            run_segment = segment
            run_start = event if segment is not None else None
            run_end = run_start
            run_event_count = 1 if run_start is not None else 0
        else:
            run_end = event
            run_event_count += 1
    flush_run()
    return samples


def _find_nearest_segment(
    point: Coordinate,
    segments: Sequence[RoadSegment],
    max_distance: float,
) -> RoadSegment | None:
    nearest: RoadSegment | None = None
    nearest_distance = math.inf
    for segment in segments:
        distance = _distance_to_segment_squared(point, segment)
        if distance < nearest_distance:
            nearest = segment
            nearest_distance = distance
    return nearest if nearest_distance <= max_distance**2 else None


def _distance_to_segment_squared(point: Coordinate, segment: RoadSegment) -> float:
    point_x = point[0] * math.cos(math.radians(point[1]))
    point_y = point[1]
    start_x = segment.start[0] * math.cos(math.radians(point[1]))
    start_y = segment.start[1]
    end_x = segment.end[0] * math.cos(math.radians(point[1]))
    end_y = segment.end[1]
    direction_x = end_x - start_x
    direction_y = end_y - start_y
    length_squared = direction_x**2 + direction_y**2
    if length_squared == 0:
        return (point_x - start_x) ** 2 + (point_y - start_y) ** 2
    projection = min(
        1.0,
        max(
            0.0,
            ((point_x - start_x) * direction_x + (point_y - start_y) * direction_y)
            / length_squared,
        ),
    )
    closest_x = start_x + projection * direction_x
    closest_y = start_y + projection * direction_y
    return (point_x - closest_x) ** 2 + (point_y - closest_y) ** 2


def _valid_segment(segment: RoadSegment) -> bool:
    return (
        bool(segment.id.strip())
        and math.isfinite(segment.ors_duration_minutes)
        and segment.ors_duration_minutes > 0
        and _valid_coordinate(segment.start)
        and _valid_coordinate(segment.end)
    )


def _valid_event(event: LocationEventPoint) -> bool:
    return _valid_coordinate(event.coordinates) and not math.isnan(
        _as_utc(event.recorded_at).timestamp()
    )


def _valid_coordinate(coordinate: Coordinate) -> bool:
    longitude, latitude = coordinate
    return (
        math.isfinite(latitude)
        and -90 <= latitude <= 90
        and math.isfinite(longitude)
        and -180 <= longitude <= 180
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
