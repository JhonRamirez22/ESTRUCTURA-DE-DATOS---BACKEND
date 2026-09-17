"""Domain model of a delivery fleet: points, stops, couriers, routes and the fleet itself."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.core.location.proximity import haversine_m


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """A WGS-84 position. Providers use ``[lng, lat]``; this class keeps the order explicit."""

    lat: float
    lng: float

    def distance_to(self, other: GeoPoint) -> float:
        """Great-circle distance in metres."""
        return haversine_m(self.lat, self.lng, other.lat, other.lng)

    def as_lng_lat(self) -> tuple[float, float]:
        return (self.lng, self.lat)


@dataclass(frozen=True, slots=True)
class Stop:
    """A delivery stop. A *locked* stop is the one the courier is already driving to."""

    stop_id: str
    location: GeoPoint
    locked: bool = False


@dataclass(frozen=True, slots=True)
class CourierState:
    courier_id: str
    location: GeoPoint


class Route:
    """An ordered list of the stops still to visit for one courier."""

    def __init__(self, route_id: str, courier: CourierState, stops: Iterable[Stop]) -> None:
        ordered = tuple(stops)
        ids = [stop.stop_id for stop in ordered]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Route {route_id} repeats a stop.")
        locked_flags = [stop.locked for stop in ordered]
        if any(locked_flags[i] and not locked_flags[i - 1] for i in range(1, len(locked_flags))):
            raise ValueError(f"Route {route_id}: locked stops must come first.")
        self.route_id = route_id
        self.courier = courier
        self.stops: tuple[Stop, ...] = ordered

    @property
    def stop_ids(self) -> tuple[str, ...]:
        return tuple(stop.stop_id for stop in self.stops)

    @property
