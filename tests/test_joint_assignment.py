"""Mejora conjunta del reparto: nunca empeora, conserva los pedidos y encuentra mejoras reales."""

from __future__ import annotations

import math
import random

import pytest

from app.core.optimization.joint_assignment import (
    MAX_ORDERS_PER_COURIER,
    estimate_cost,
    improve_assignment,
)


def _instance(
    couriers: int, orders: int, rng: random.Random
) -> tuple[list[list[float | None]], dict[str, int], dict[str, int]]:
    points = [(rng.random() * 40, rng.random() * 40) for _ in range(couriers + orders)]
    matrix: list[list[float | None]] = [
        [0.0 if a == b else abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) for b, pb in enumerate(points)]
        for a, pa in enumerate(points)
    ]
    courier_index = {f"c{index}": index for index in range(couriers)}
    order_index = {f"o{index}": couriers + index for index in range(orders)}
    return matrix, courier_index, order_index


def _greedy(
    matrix: list[list[float | None]], courier_index: dict[str, int], order_index: dict[str, int]
) -> dict[str, list[str]]:
    """Lo que hace assign_orders: cada pedido al repartidor más cercano, sin mirar la ruta."""
    result: dict[str, list[str]] = {courier: [] for courier in courier_index}
    for order, index in order_index.items():
        nearest = min(courier_index, key=lambda c: matrix[courier_index[c]][index] or math.inf)
        result[nearest].append(order)
    return result


def test_traslada_un_pedido_lejano_al_repartidor_que_ya_pasa_cerca() -> None:
    # Línea: A en 0 y B en 100. Los pedidos 10, 12 y 14 son de A; el 90 está al lado de B.
    positions = [0, 100, 10, 12, 14, 90]
    matrix: list[list[float | None]] = [[float(abs(a - b)) for b in positions] for a in positions]
    courier_index = {"A": 0, "B": 1}
    order_index = {"p10": 2, "p12": 3, "p14": 4, "p90": 5}
    start = {"A": ["p10", "p12", "p14", "p90"], "B": []}
    result = improve_assignment(matrix, courier_index, order_index, start)
    assert "p90" in result.assignments["B"]
    assert result.after.total_minutes < result.before.total_minutes
    assert result.moves and result.moves[0].gain > 0


def test_intercambia_dos_pedidos_cuando_ninguno_solo_basta() -> None:
