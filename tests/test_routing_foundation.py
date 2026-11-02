"""Routing toolkit: domain, cost matrix, spatial grid, solvers, scoreboard and re-optimiser."""

from __future__ import annotations

import math
import random

import pytest

from app.core.structures.ttl_cache import TtlCache
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import CourierState, Fleet, GeoPoint, Route, Stop
from app.routing.reoptimizer import LiveFleetReoptimizer
from app.routing.scoreboard import PlanScoreboard, SegmentCrossingCounter
from app.routing.solvers import NearestNeighborSolver, RoutePipeline
from app.routing.spatial_grid import SpatialGrid

SPEED_M_PER_MIN = 400.0  # about 24 km/h through Pasto's streets


def point(lat: float, lng: float) -> GeoPoint:
    return GeoPoint(lat, lng)


def matrix_for(fleet: Fleet) -> CostMatrix:
    points = fleet.index().points
    rows = [[0.0 if a is b else a.distance_to(b) / SPEED_M_PER_MIN for b in points] for a in points]
    return CostMatrix(rows)


def route(route_id: str, courier: GeoPoint, stops: list[tuple[str, float, float]], locked: int = 0):
    items = [
        Stop(sid, point(lat, lng), locked=i < locked) for i, (sid, lat, lng) in enumerate(stops)
    ]
    return Route(route_id, CourierState(f"c-{route_id}", courier), items)


# ---- structures --------------------------------------------------------------------------


def test_ttl_cache_expires_entries_and_keeps_lru_bounds() -> None:
    now = [0.0]
    cache: TtlCache[str, int] = TtlCache(2, 10.0, clock=lambda: now[0])
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    now[0] = 9.9
    assert cache.get("b") == 2
    now[0] = 10.0
    assert cache.get("a") is None and cache.get("b") is None
    cache.put("c", 3)
    cache.put("d", 4)
    cache.put("e", 5)
    assert len(cache) == 2 and cache.get("c") is None
    with pytest.raises(ValueError):
        TtlCache(1, 0)


def test_spatial_grid_matches_brute_force_nearest() -> None:
    rng = random.Random(7)
    grid: SpatialGrid[int] = SpatialGrid(300.0)
    items = {
        i: point(1.20 + rng.random() * 0.04, -77.30 + rng.random() * 0.06) for i in range(150)
    }
    for item_id, location in items.items():
        grid.insert(item_id, location)
    for _ in range(25):
        query = point(1.20 + rng.random() * 0.04, -77.30 + rng.random() * 0.06)
        expected = sorted(((query.distance_to(p), i) for i, p in items.items()))[:5]
        got = grid.nearest(query, 5)
        assert [i for i, _ in got] == [i for _, i in expected]
        pairs = zip(got, expected, strict=True)
        assert all(math.isclose(a[1], b[0], rel_tol=1e-9) for a, b in pairs)


