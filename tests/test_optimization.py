from __future__ import annotations

import pytest

from app.core.optimization.assign_orders import (
    MAX_ASSIGNABLE_ORDERS_PER_ROUTE,
    AssignmentAdjustment,
    AssignOrdersOptions,
    Coordinates,
    CourierPosition,
    OrderPoint,
    assign_orders,
)
from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.two_opt import two_opt


def _courier(
    courier_id: str,
    lat: float,
    lng: float,
    available: bool = True,
) -> CourierPosition:
    return CourierPosition(courier_id, Coordinates(lat, lng), available)


def _order(order_id: str, lat: float, lng: float) -> OrderPoint:
    return OrderPoint(order_id, Coordinates(lat, lng))


def test_nearest_neighbor_handles_empty_singleton_and_null_edges() -> None:
    assert nearest_neighbor([], 0) == []
    assert nearest_neighbor([[0]], 0) == [0]
    matrix: list[list[float | None]] = [
        [0, None, 10],
        [None, 0, 1],
        [10, 1, 0],
    ]
    assert nearest_neighbor(matrix, 0) == [0, 2, 1]


def test_nearest_neighbor_selects_lowest_next_cost_and_checks_inputs() -> None:
    matrix = [[0, 2, 8], [2, 0, 3], [8, 3, 0]]
    assert nearest_neighbor(matrix, 0) == [0, 1, 2]
    with pytest.raises(IndexError, match="start_index"):
        nearest_neighbor(matrix, 3)
    with pytest.raises(ValueError, match="cuadrada"):
        nearest_neighbor([[0, 1]], 0)


def test_two_opt_handles_short_routes_and_relocates_final_stop() -> None:
    matrix = [
        [0, 10, 10, 1],
        [10, 0, 1, 10],
        [10, 1, 0, 10],
        [1, 10, 10, 0],
    ]
    assert two_opt(matrix, [0, 1, 2]) == [0, 1, 2]
    route = two_opt(matrix, [0, 1, 2, 3])
    assert route == [0, 3, 2, 1]
    assert sum(matrix[left][right] for left, right in zip(route, route[1:], strict=False)) == 12


def test_two_opt_treats_missing_edges_as_infinite_and_rejects_duplicate_stops() -> None:
    matrix: list[list[float | None]] = [
        [0, 10, None, 20],
        [10, 0, 10, 1],
