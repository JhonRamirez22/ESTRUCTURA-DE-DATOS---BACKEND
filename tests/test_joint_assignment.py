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
    positions = [0, 100, 90, 10]  # A en 0, B en 100; pedido x en 90 (de A) y pedido y en 10 (de B)
    matrix: list[list[float | None]] = [[float(abs(a - b)) for b in positions] for a in positions]
    result = improve_assignment(
        matrix, {"A": 0, "B": 1}, {"x": 2, "y": 3}, {"A": ["x"], "B": ["y"]}
    )
    assert result.assignments == {"A": ["y"], "B": ["x"]}
    assert result.moves[0].kind == "swap"


@pytest.mark.parametrize("seed", range(25))
def test_nunca_empeora_conserva_pedidos_y_respeta_el_tope(seed: int) -> None:
    rng = random.Random(seed)
    couriers = rng.randint(2, 5)
    orders = rng.randint(4, 24)
    matrix, courier_index, order_index = _instance(couriers, orders, rng)
    start = _greedy(matrix, courier_index, order_index)
    result = improve_assignment(matrix, courier_index, order_index, start)
    assert result.after.objective() <= result.before.objective() + 1e-9
    flat = sorted(order for route in result.assignments.values() for order in route)
    assert flat == sorted(order_index)
    assert all(len(route) <= MAX_ORDERS_PER_COURIER for route in result.assignments.values())
    assert result.after == estimate_cost(matrix, courier_index, order_index, result.assignments)


def test_mejora_de_verdad_en_una_parte_de_los_repartos_por_cercania() -> None:
    improved = 0
    for seed in range(40):
        rng = random.Random(1000 + seed)
        matrix, courier_index, order_index = _instance(3, 15, rng)
        start = _greedy(matrix, courier_index, order_index)
        result = improve_assignment(matrix, courier_index, order_index, start)
        improved += result.after.objective() < result.before.objective() - 0.5
    assert improved >= 12  # el reparto por cercanía deja ahorros reales sobre la mesa


def test_es_determinista() -> None:
    rng = random.Random(7)
    matrix, courier_index, order_index = _instance(3, 14, rng)
    start = _greedy(matrix, courier_index, order_index)
    first = improve_assignment(matrix, courier_index, order_index, start)
    second = improve_assignment(matrix, courier_index, order_index, start)
    assert first.assignments == second.assignments


def test_no_manda_un_pedido_a_quien_no_puede_llegar() -> None:
    positions = [0, 100, 10, 90]
    matrix: list[list[float | None]] = [[float(abs(a - b)) for b in positions] for a in positions]
    matrix[1][3] = None  # B no llega al pedido 90
    matrix[3][1] = None
    result = improve_assignment(
        matrix, {"A": 0, "B": 1}, {"p10": 2, "p90": 3}, {"A": ["p10", "p90"], "B": []}
    )
    assert "p90" not in result.assignments["B"]


def test_sin_repartidores_o_con_uno_no_cambia_nada() -> None:
    matrix: list[list[float | None]] = [[0.0, 5.0], [5.0, 0.0]]
    result = improve_assignment(matrix, {"A": 0}, {"p": 1}, {"A": ["p"]})
    assert result.assignments == {"A": ["p"]} and result.moves == []
    empty = improve_assignment(matrix, {"A": 0}, {"p": 1}, {})
    assert empty.assignments == {}


def test_never_piles_every_order_on_one_courier_even_if_that_minimises_total_minutes() -> None:
    from app.core.optimization.joint_assignment import improve_assignment

    # 4 repartidores lejos (14 min a cualquier pedido) y pedidos pegados entre sí (1 min):
    # el costo total mínimo es un solo repartidor con todo, pero eso anula el reparto.
    couriers, orders = 4, 12
    size = couriers + orders
    matrix: list[list[float | None]] = [[0.0] * size for _ in range(size)]
    for a in range(size):
        for b in range(size):
            if a == b:
                continue
            courier_leg = a < couriers or b < couriers
            matrix[a][b] = 14.0 if courier_leg else 1.0
    courier_index = {f"c{i}": i for i in range(couriers)}
    order_index = {f"o{i}": couriers + i for i in range(orders)}
    start = {f"c{i}": [f"o{3 * i + k}" for k in range(3)] for i in range(couriers)}
    result = improve_assignment(matrix, courier_index, order_index, start)
    loads = sorted(len(v) for v in result.assignments.values())
    assert max(loads) <= 4  # floor(12/4 · 1,5)
    assert sum(loads) == orders
    assert result.after.objective() <= result.before.objective() + 1e-9


def test_real_collapse_case_keeps_the_load_shared_and_everyone_busy() -> None:
    """Matriz real (4 repartidores, 14 pedidos) donde antes quedaba [0, 0, 0, 14]."""
    import json
    from pathlib import Path

    from app.core.optimization.joint_assignment import improve_assignment

    data = json.loads(
        (Path(__file__).parent / "fixtures" / "joint_assignment_collapse.json").read_text()
    )
    couriers = data["couriers"]
    courier_index = {f"c{i}": i for i in range(couriers)}
    orders = sum(len(group) for group in data["start"])
    order_index = {f"o{i}": couriers + i for i in range(orders)}
    start = {f"c{i}": [f"o{o}" for o in group] for i, group in enumerate(data["start"])}
    result = improve_assignment(data["matrix"], courier_index, order_index, start)
    loads = [len(v) for v in result.assignments.values()]
    assert sum(loads) == orders
    assert max(loads) <= 5  # floor(14 / 4 · 1,5)
    assert min(loads) >= 1  # nadie queda sin pedidos
    assert result.after.objective() <= result.before.objective() + 1e-9
