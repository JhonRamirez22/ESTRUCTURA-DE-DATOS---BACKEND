"""Synthetic Pasto scenarios and a runner that measures plan quality against a baseline.

The baseline is the pipeline that dispatches today: nearest-courier assignment, the joint
relocate/swap improvement and a per-route solver pipeline. The live re-optimiser is then run
on top of it to measure how much it still finds. Every scenario is deterministic (seeded).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal

from app.core.optimization.assign_orders import (
    AssignOrdersOptions,
    Coordinates,
    CourierPosition,
    OrderPoint,
    assign_orders,
)
from app.core.optimization.joint_assignment import improve_assignment
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import CourierState, Fleet, GeoPoint, Route, Stop
from app.routing.reoptimizer import LiveFleetReoptimizer
from app.routing.scoreboard import PlanScore
from app.routing.solvers import RoutePipeline

Layout = Literal["clustered", "uniform"]
STREET_SPEED_M_PER_MIN = 400.0
# Pasto's service area (lat, lng); barrios used as cluster centres.
_BOUNDS = (1.195, 1.235, -77.300, -77.255)
_BARRIOS = ((1.2136, -77.2811), (1.2172, -77.2744), (1.2260, -77.2870), (1.2050, -77.2790))
COURIER_COUNTS = (2, 4, 6)
STOP_COUNTS = (8, 14, 25, 35, 50)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    layout: Layout
    couriers: tuple[GeoPoint, ...]
    stops: tuple[GeoPoint, ...]


class ScenarioGenerator:
    """Builds the deterministic battery: every courier count x stop count x layout."""

    def __init__(self, seed: int = 2026) -> None:
        self._seed = seed

    def battery(self) -> list[Scenario]:
        scenarios: list[Scenario] = []
        for couriers in COURIER_COUNTS:
            for stops in STOP_COUNTS:
                for layout in ("clustered", "uniform"):
                    scenarios.append(self.scenario(couriers, stops, layout))
        return scenarios

    def scenario(self, couriers: int, stops: int, layout: Layout) -> Scenario:
        rng = random.Random(f"{self._seed}:{couriers}:{stops}:{layout}")
        lat_min, lat_max, lng_min, lng_max = _BOUNDS

        def uniform() -> GeoPoint:
            return GeoPoint(rng.uniform(lat_min, lat_max), rng.uniform(lng_min, lng_max))

        def clustered() -> GeoPoint:
            lat, lng = rng.choice(_BARRIOS)
            return GeoPoint(
                min(lat_max, max(lat_min, rng.gauss(lat, 0.004))),
                min(lng_max, max(lng_min, rng.gauss(lng, 0.004))),
            )

        make = clustered if layout == "clustered" else uniform
        return Scenario(
            f"{couriers}c-{stops}s-{layout}",
            layout,
            tuple(uniform() for _ in range(couriers)),
            tuple(make() for _ in range(stops)),
        )


def street_matrix(points: list[GeoPoint]) -> CostMatrix:
    """Minutes through a street grid: Manhattan distance at a typical city speed."""
    ref_cos = math.cos(math.radians(1.21))

    def minutes(a: GeoPoint, b: GeoPoint) -> float:
        dy = abs(a.lat - b.lat) * 111_320.0
        dx = abs(a.lng - b.lng) * 111_320.0 * ref_cos
        return (dx + dy) / STREET_SPEED_M_PER_MIN

    return CostMatrix([[0.0 if a is b else minutes(a, b) for b in points] for a in points])


@dataclass(frozen=True, slots=True)
class BenchmarkRow:
    scenario: str
    baseline: PlanScore
    reoptimized: PlanScore

    @property
    def objective_gain(self) -> float:
        return self.baseline.objective() - self.reoptimized.objective()


class BaselinePlanner:
    """The dispatch pipeline as it is today, run on a synthetic scenario."""

    def plan(self, scenario: Scenario) -> tuple[Fleet, CostMatrix]:
        points = [*scenario.couriers, *scenario.stops]
        matrix = street_matrix(points)
        c_count = len(scenario.couriers)
        courier_ids = [f"c{i}" for i in range(c_count)]
        stop_ids = [f"s{i}" for i in range(len(scenario.stops))]
        travel = {
            (courier_ids[c], stop_ids[s]): matrix.cost(c, c_count + s)
            for c in range(c_count)
            for s in range(len(stop_ids))
        }
        assigned = assign_orders(
            [
                CourierPosition(cid, Coordinates(p.lat, p.lng))
                for cid, p in zip(courier_ids, scenario.couriers, strict=True)
            ],
            [
                OrderPoint(sid, Coordinates(p.lat, p.lng))
                for sid, p in zip(stop_ids, scenario.stops, strict=True)
            ],
            AssignOrdersOptions(travel_costs_minutes=travel),
