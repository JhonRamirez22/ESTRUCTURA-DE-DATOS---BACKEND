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
