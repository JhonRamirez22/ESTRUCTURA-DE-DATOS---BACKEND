from __future__ import annotations

import math

from app.core.optimization.order_verifier import (
    build_cost_graph,
    effective_route_cost,
    is_valid_order,
    verify_candidate_order,
)

# 0 es el origen. 0->1->2->3 cuesta 30; 0->2->1->3 cuesta 12.
MATRIX = [
    [0, 10, 3, 20],
    [10, 0, 10, 4],
    [3, 5, 0, 12],
    [20, 4, 12, 0],
]


def test_valid_order_keeps_origin_and_visits_every_stop_once() -> None:
    assert is_valid_order([0, 2, 1, 3], [0, 1, 2, 3])
    assert not is_valid_order([1, 0, 2, 3], [0, 1, 2, 3])
    assert not is_valid_order([0, 1, 1, 3], [0, 1, 2, 3])
    assert not is_valid_order([0, 1, 2], [0, 1, 2, 3])


def test_effective_cost_uses_shortest_paths_between_consecutive_stops() -> None:
    graph = build_cost_graph(MATRIX)
    # 0->1 directo cuesta 10, pero 0->2->1 cuesta 8: Dijkstra encuentra el atajo.
    assert effective_route_cost(graph, [0, 1]) == 8
    assert effective_route_cost(graph, [0]) == 0


def test_candidate_is_accepted_only_when_it_saves_time() -> None:
    better = verify_candidate_order(MATRIX, [0, 1, 2, 3], [0, 2, 1, 3])
    assert better.accepted and better.saving_minutes > 0.5
    worse = verify_candidate_order(MATRIX, [0, 2, 1, 3], [0, 1, 2, 3])
    assert not worse.accepted and worse.reason == "no mejora el costo"
    same = verify_candidate_order(MATRIX, [0, 1, 2, 3], [0, 1, 2, 3])
    assert not same.accepted


def test_invalid_or_disconnected_candidates_are_rejected() -> None:
    assert verify_candidate_order(MATRIX, [0, 1, 2, 3], [0, 1, 1, 3]).reason == "orden inválido"
    broken = [[0, None, None], [None, 0, None], [None, None, 0]]
    result = verify_candidate_order(broken, [0, 1, 2], [0, 2, 1])
    assert not result.accepted and math.isinf(result.candidate_cost)
