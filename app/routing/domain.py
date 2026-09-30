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
    def locked_count(self) -> int:
        return sum(1 for stop in self.stops if stop.locked)

    def with_stops(self, stops: Iterable[Stop]) -> Route:
        return Route(self.route_id, self.courier, stops)

    def __len__(self) -> int:
        return len(self.stops)


@dataclass(frozen=True, slots=True)
class FleetIndex:
    """Maps couriers and stops to rows of the shared cost matrix.

    Rows ``0..R-1`` are the couriers (in route order) and the rest are the stops of every
    route, so a single road-matrix call serves the whole fleet.
    """

    points: tuple[GeoPoint, ...]
    courier_node: dict[str, int]
    stop_node: dict[str, int]


class Fleet:
    """A set of routes optimised together."""

    def __init__(self, routes: Sequence[Route]) -> None:
        ids = [route.route_id for route in routes]
        if len(set(ids)) != len(ids):
            raise ValueError("Fleet routes must have unique ids.")
        seen: set[str] = set()
        for route in routes:
            for stop_id in route.stop_ids:
                if stop_id in seen:
                    raise ValueError(f"Stop {stop_id} appears in more than one route.")
                seen.add(stop_id)
        self.routes: tuple[Route, ...] = tuple(routes)

    def index(self) -> FleetIndex:
        points: list[GeoPoint] = []
        courier_node: dict[str, int] = {}
        stop_node: dict[str, int] = {}
        for route in self.routes:
            courier_node[route.route_id] = len(points)
            points.append(route.courier.location)
        for route in self.routes:
            for stop in route.stops:
                stop_node[stop.stop_id] = len(points)
                points.append(stop.location)
        return FleetIndex(tuple(points), courier_node, stop_node)

    def route(self, route_id: str) -> Route:
        for route in self.routes:
            if route.route_id == route_id:
                return route
        raise KeyError(route_id)

    def __len__(self) -> int:
        return len(self.routes)
