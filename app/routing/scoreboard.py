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
