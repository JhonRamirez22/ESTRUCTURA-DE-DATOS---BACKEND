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
        [None, 10, 0, 10],
        [20, 1, 10, 0],
    ]
    assert two_opt(matrix, [0, 1, 2, 3]) == [0, 1, 3, 2]
    with pytest.raises(ValueError, match="repetir"):
        two_opt(matrix, [0, 1, 1])


def test_assign_orders_handles_zero_couriers_and_zero_orders() -> None:
    assert assign_orders([], [_order("x", 1, 1)]) == {}
    assert assign_orders([_courier("offline", 0, 0, False)], [_order("x", 1, 1)]) == {}
    assert assign_orders([_courier("available", 0, 0)], []) == {"available": []}


def test_assign_orders_uses_nearest_courier_and_single_courier() -> None:
    result = assign_orders(
        [_courier("north", 10, 10), _courier("south", 0, 0)],
        [_order("n1", 9, 9), _order("s1", 1, 1)],
    )
    assert result == {"north": ["n1"], "south": ["s1"]}

    single = assign_orders(
        [_courier("only", 0, 0)],
        [_order("first", 1, 1), _order("second", 2, 2)],
    )
    assert single == {"only": ["first", "second"]}


def test_assign_orders_caps_load_and_balances_large_cluster() -> None:
    orders = [_order(f"order-{index}", index / 100, index / 100) for index in range(10)]
    balanced = assign_orders([_courier("near", 0, 0), _courier("far", 80, 170)], orders)
    assert [len(items) for items in balanced.values()] == [7, 3]

    excess = [
        _order(f"order-{index}", 1, 1) for index in range(MAX_ASSIGNABLE_ORDERS_PER_ROUTE + 1)
    ]
    capped = assign_orders([_courier("only", 0, 0)], excess)
    assert len(capped["only"]) == MAX_ASSIGNABLE_ORDERS_PER_ROUTE


def test_assign_orders_applies_strict_load_ratio_for_small_and_odd_order_counts() -> None:
    three_orders = assign_orders(
        [_courier("first", 0, 0), _courier("second", 0, 0)],
        [_order(f"order-{index}", 1, 1) for index in range(3)],
    )
    assert sorted(map(len, three_orders.values())) == [1, 2]


def test_assign_orders_validates_traffic_factors_and_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="traffic_multiplier"):
        assign_orders(
            [_courier("near", 0, 0)],
            [_order("x", 0, 1)],
            AssignOrdersOptions((AssignmentAdjustment("near", "x", 10),)),
        )
    with pytest.raises(ValueError, match="order_id"):
        assign_orders([], [_order("x", 0, 0), _order("x", 1, 1)])


def test_assign_orders_applies_congestion_multiplier_to_clustering_cost() -> None:
    result = assign_orders(
        [_courier("near", 0, 0), _courier("far", 0, 10)],
        [_order("x", 0, 4)],
        AssignOrdersOptions((AssignmentAdjustment("near", "x", 3),)),
    )
    assert result == {"near": [], "far": ["x"]}
