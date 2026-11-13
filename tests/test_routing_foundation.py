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


def test_spatial_grid_radius_exclude_remove_and_empty() -> None:
    grid: SpatialGrid[str] = SpatialGrid()
    assert grid.nearest(point(1.2, -77.2), 3) == []
    grid.insert("a", point(1.2000, -77.2000))
    grid.insert("b", point(1.2001, -77.2000))
    grid.insert("far", point(1.3000, -77.2000))
    near = grid.nearest(point(1.2000, -77.2000), 5, max_metres=500, exclude="a")
    assert [i for i, _ in near] == ["b"]
    grid.remove("b")
    assert "b" not in grid and len(grid) == 2
    assert [i for i, _ in grid.nearest(point(1.2, -77.2), 5)] == ["a", "far"]
    grid.insert("a", point(1.2500, -77.2))  # re-inserting moves it
    assert len(grid) == 2


# ---- domain and matrix -------------------------------------------------------------------


def test_domain_validation_and_fleet_index() -> None:
    courier = point(1.21, -77.28)
    with pytest.raises(ValueError):
        route("r", courier, [("s", 1.2, -77.2), ("s", 1.3, -77.3)])
    with pytest.raises(ValueError):  # a locked stop after an unlocked one
        Route(
            "r", CourierState("c", courier), [Stop("a", courier), Stop("b", courier, locked=True)]
        )
    one = route("r1", courier, [("a", 1.2, -77.2)])
    two = route("r2", courier, [("a", 1.3, -77.3)])
    with pytest.raises(ValueError):
        Fleet([one, two])  # the same stop in two routes
    fleet = Fleet([one, route("r2", courier, [("b", 1.3, -77.3)])])
    index = fleet.index()
    assert index.courier_node == {"r1": 0, "r2": 1} and index.stop_node == {"a": 2, "b": 3}
    assert len(index.points) == 4


def test_cost_matrix_rejects_bad_input_and_treats_none_as_unreachable() -> None:
    with pytest.raises(ValueError):
        CostMatrix([[0, 1], [1]])
    with pytest.raises(ValueError):
        CostMatrix([[0, -1], [1, 0]])
    matrix = CostMatrix([[0, None, 3], [2, 0, 1], [4, 5, 0]])
    assert matrix.cost(0, 1) == math.inf and matrix.path_cost([0, 1, 2]) == math.inf
    assert matrix.path_cost([0, 2, 1]) == 3 + 5
    assert matrix.submatrix([2, 0]).rows == [[0.0, 4], [3, 0.0]]


# ---- solvers -----------------------------------------------------------------------------


def random_matrix(rng: random.Random, size: int) -> CostMatrix:
    coords = [(rng.random(), rng.random()) for _ in range(size)]
    return CostMatrix([[math.dist(a, b) * 10 for b in coords] for a in coords])


@pytest.mark.parametrize("seed", range(25))
def test_pipeline_never_worse_keeps_origin_and_nodes(seed: int) -> None:
    rng = random.Random(seed)
    size = rng.randint(3, 11)
    matrix = random_matrix(rng, size)
    order = [0, *rng.sample(range(1, size), size - 1)]
    result = RoutePipeline.standard().improve(matrix, order)
    assert result.order[0] == 0 and sorted(result.order) == sorted(order)
    assert result.cost <= matrix.path_cost(order) + 1e-9
    assert math.isclose(result.cost, matrix.path_cost(result.order))


def test_pipeline_handles_tiny_and_disconnected_routes() -> None:
    matrix = CostMatrix([[0, 1], [1, 0]])
    assert RoutePipeline.standard().improve(matrix, [0, 1]).order == [0, 1]
    broken = CostMatrix([[0, None, None], [None, 0, 1], [None, 2, 0]])
    result = RoutePipeline.standard().improve(broken, [0, 1, 2])
    assert result.order[0] == 0 and sorted(result.order) == [0, 1, 2]
    with pytest.raises(ValueError):
        RoutePipeline([])
    assert NearestNeighborSolver().improve(matrix, [0, 1]) == [0, 1]


# ---- scoreboard --------------------------------------------------------------------------


def test_crossing_counter_finds_an_x_and_ignores_touching_segments() -> None:
    counter = SegmentCrossingCounter()
    x_shape = [[point(0, 0), point(1, 1)], [point(0, 1), point(1, 0)]]
    assert counter.count(x_shape) == 1
    parallel = [[point(0, 0), point(0, 1)], [point(1, 0), point(1, 1)]]
    assert counter.count(parallel) == 0
    bow_tie = [[point(0, 0), point(1, 1), point(1, 0), point(0, 1)]]
    assert counter.count(bow_tie) == 1  # segments 0 and 2 cross; neighbours are skipped


def test_scoreboard_reports_balance_total_and_gap() -> None:
    fleet = Fleet([
        route("a", point(1.2000, -77.3000), [("a1", 1.2005, -77.2990), ("a2", 1.2010, -77.2980)]),
        route("b", point(1.2000, -77.2500), [("b1", 1.2005, -77.2490)]),
    ])
    scoreboard = PlanScoreboard(fleet, matrix_for(fleet))
    score = scoreboard.score(scoreboard.current_orders())
    assert score.total_minutes > 0 and score.longest_minutes >= score.total_minutes / 2
    assert score.balance >= 1.0 and score.crossings == 0
    assert score.lower_bound_gap is not None and score.lower_bound_gap >= 0
    assert set(score.as_dict()) == {
        "totalMinutes", "longestMinutes", "balance", "crossings", "lowerBoundGap"
    }


# ---- re-optimiser ------------------------------------------------------------------------


def test_reorders_a_zigzag_route_and_reports_the_saving() -> None:
    fleet = Fleet([route("a", point(1.2000, -77.3000), [
        ("s3", 1.2000, -77.2940), ("s1", 1.2000, -77.2990), ("s2", 1.2000, -77.2970),
    ])])
    proposal = LiveFleetReoptimizer(fleet, matrix_for(fleet)).propose()
    only = proposal.routes[0]
    assert only.changed and only.proposed_order == ("s1", "s2", "s3")
    assert only.saved_minutes > 1 and proposal.saved_minutes > 1 and not proposal.moves


def test_keeps_an_optimal_fleet_untouched() -> None:
    fleet = Fleet([
        route("a", point(1.2000, -77.3000), [("a1", 1.2000, -77.2990), ("a2", 1.2000, -77.2980)]),
        route("b", point(1.2000, -77.2500), [("b1", 1.2000, -77.2490), ("b2", 1.2000, -77.2480)]),
    ])
    proposal = LiveFleetReoptimizer(fleet, matrix_for(fleet)).propose()
    assert not proposal.changed and not proposal.moves


def test_moves_a_stop_to_the_courier_that_is_next_to_it() -> None:
    fleet = Fleet([
        route("a", point(1.2000, -77.3000), [
            ("a1", 1.2005, -77.2990), ("a2", 1.2010, -77.2980), ("x", 1.2005, -77.2505),
        ]),
        route("b", point(1.2000, -77.2500), [("b1", 1.2000, -77.2490), ("b2", 1.2010, -77.2480)]),
    ])
    proposal = LiveFleetReoptimizer(fleet, matrix_for(fleet)).propose()
    assert [(m.stop_id, m.from_route, m.to_route) for m in proposal.moves] == [("x", "a", "b")]
    by_id = {p.route_id: p for p in proposal.routes}
    assert "x" in by_id["b"].proposed_order and "x" not in by_id["a"].proposed_order
    assert by_id["b"].moved_in == ("x",) and by_id["a"].moved_out == ("x",)
    assert proposal.score_after.objective() < proposal.score_before.objective()


def test_locked_stop_stays_first_and_never_moves() -> None:
    fleet = Fleet([
        route("a", point(1.2000, -77.3000), [
            ("x", 1.2005, -77.2505), ("a1", 1.2005, -77.2990), ("a2", 1.2010, -77.2980),
        ], locked=1),
        route("b", point(1.2000, -77.2500), [("b1", 1.2000, -77.2490), ("b2", 1.2010, -77.2480)]),
    ])
