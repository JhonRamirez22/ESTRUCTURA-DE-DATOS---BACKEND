"""Plan quality metrics: minutes, balance, route crossings and gap to a lower bound."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.optimization.lower_bound import spanning_tree_lower_bound
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import Fleet, GeoPoint


@dataclass(frozen=True, slots=True)
class PlanScore:
    """Quality of a whole fleet plan; lower is better except ``balance`` (1.0 is perfect)."""

    total_minutes: float
    longest_minutes: float
    balance: float
    crossings: int
    lower_bound_gap: float | None

    def objective(self, makespan_weight: float = 0.5) -> float:
        return self.total_minutes + makespan_weight * self.longest_minutes

    def as_dict(self) -> dict[str, float | int | None]:
        gap = None if self.lower_bound_gap is None else round(self.lower_bound_gap, 3)
        return {
            "totalMinutes": round(self.total_minutes, 1),
            "longestMinutes": round(self.longest_minutes, 1),
            "balance": round(self.balance, 2),
            "crossings": self.crossings,
            "lowerBoundGap": gap,
        }


class SegmentCrossingCounter:
    """Counts proper intersections between straight segments drawn between consecutive stops."""

    @staticmethod
    def _orientation(a: GeoPoint, b: GeoPoint, c: GeoPoint) -> float:
        return (b.lng - a.lng) * (c.lat - a.lat) - (b.lat - a.lat) * (c.lng - a.lng)

    @classmethod
    def intersect(cls, p1: GeoPoint, p2: GeoPoint, q1: GeoPoint, q2: GeoPoint) -> bool:
        d1 = cls._orientation(p1, p2, q1)
        d2 = cls._orientation(p1, p2, q2)
        d3 = cls._orientation(q1, q2, p1)
        d4 = cls._orientation(q1, q2, p2)
        return d1 * d2 < 0 and d3 * d4 < 0

    def count(self, polylines: Sequence[Sequence[GeoPoint]]) -> int:
        segments: list[tuple[int, int, GeoPoint, GeoPoint]] = []
        for route_index, line in enumerate(polylines):
            for index, (a, b) in enumerate(zip(line, line[1:], strict=False)):
                segments.append((route_index, index, a, b))
        total = 0
        for i, (route_i, index_i, a, b) in enumerate(segments):
            for route_j, index_j, c, d in segments[i + 1 :]:
                if route_i == route_j and abs(index_i - index_j) <= 1:
                    continue  # consecutive segments of one route share an endpoint
                if self.intersect(a, b, c, d):
                    total += 1
        return total


class PlanScoreboard:
    """Scores a fleet given the order of stops of each route and a shared cost matrix."""

    def __init__(self, fleet: Fleet, matrix: CostMatrix) -> None:
        self._fleet = fleet
        self._matrix = matrix
        self._index = fleet.index()

    def score(self, orders: Mapping[str, Sequence[str]]) -> PlanScore:
        """``orders`` maps each route id to its ordered stop ids (moved stops allowed)."""
        minutes: list[float] = []
        polylines: list[list[GeoPoint]] = []
        bounds: list[float] = []
        for route in self._fleet.routes:
            stop_ids = orders[route.route_id]
            nodes = [self._index.courier_node[route.route_id]]
            nodes += [self._index.stop_node[stop_id] for stop_id in stop_ids]
            minutes.append(self._matrix.path_cost(nodes))
            polylines.append([self._index.points[node] for node in nodes])
            bound = spanning_tree_lower_bound(self._matrix.submatrix(nodes).rows)
            if bound is not None and bound > 0:
                bounds.append(bound)
        active = [value for value in minutes if value > 0 and math.isfinite(value)]
        total = sum(active)
        longest = max(active, default=0.0)
        mean = total / len(active) if active else 0.0
        gap = None
        if bounds and len(bounds) == len([m for m in minutes if m > 0]):
            gap = max(0.0, total / sum(bounds) - 1.0)
        return PlanScore(
            total_minutes=total,
            longest_minutes=longest,
            balance=(longest / mean) if mean > 0 else 1.0,
            crossings=SegmentCrossingCounter().count(polylines),
            lower_bound_gap=gap,
        )

    def current_orders(self) -> dict[str, tuple[str, ...]]:
        return {route.route_id: route.stop_ids for route in self._fleet.routes}
