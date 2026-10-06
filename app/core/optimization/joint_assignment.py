"""Mejora conjunta del reparto: mueve pedidos entre repartidores para acortar las rutas.

``assign_orders`` reparte por cercanía al repartidor y balancea cargas, pero mira cada pedido por
separado: dos pedidos vecinos pueden acabar en rutas distintas aunque una sola pasada por ambos
costara casi nada. Aquí se parte de ese reparto y se aplican movimientos (trasladar un pedido o
intercambiar dos) mientras bajen el costo conjunto:

    objetivo = minutos totales de todas las rutas + peso · minutos de la ruta más larga

El segundo término evita «ahorrar» cargando a un solo repartidor. Cada movimiento se evalúa con el
costo estimado de la ruta de cada repartidor sobre la misma matriz vial (vecino más cercano más
búsqueda local, ver ``refine``); un ``MinHeap`` ordena los movimientos candidatos y se aplica el
que más baja el objetivo. Nunca empeora el reparto de partida.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.refine import local_search, route_cost
from app.core.structures.min_heap import MinHeap

MAKESPAN_WEIGHT = 0.5
MAX_PASSES = 25
MAX_EVALUATIONS = 4_000
MAX_ORDERS_PER_COURIER = 50
# Mismo criterio de balance que ``assign_orders``: nadie recibe más de 1,5 veces la carga media.
LOAD_CAP_MULTIPLIER = 1.5
MIN_GAIN_MINUTES = 0.05
NEIGHBOURS_FOR_SWAPS = 6

Matrix = Sequence[Sequence[float | None]]


@dataclass(frozen=True, slots=True)
class AssignmentCost:
    total_minutes: float
    longest_route_minutes: float

    def objective(self, weight: float = MAKESPAN_WEIGHT) -> float:
        return self.total_minutes + weight * self.longest_route_minutes


@dataclass(frozen=True, slots=True)
class Move:
    """Un movimiento aplicado: ``order_id`` pasó de ``from_courier`` a ``to_courier``."""

    kind: str  # "relocate" | "swap"
    order_id: str
    from_courier: str
    to_courier: str
    swapped_order_id: str | None
    gain: float  # reducción del objetivo, en minutos


@dataclass(frozen=True, slots=True)
class JointAssignmentResult:
    assignments: dict[str, list[str]]
    before: AssignmentCost
    after: AssignmentCost
    moves: list[Move]
    evaluations: int

    @property
    def saved_total_minutes(self) -> float:
        return self.before.total_minutes - self.after.total_minutes


@dataclass(frozen=True, slots=True)
class _Candidate:
    gain: float
    key: tuple[object, ...]
    move: Move


def _compare(left: _Candidate, right: _Candidate) -> int:
    # Menor «gain negativo» primero = mayor ganancia primero; el desempate es estable.
    if left.gain != right.gain:
        return -1 if left.gain > right.gain else 1
    return (left.key > right.key) - (left.key < right.key)


class _Evaluator:
    """Costo de la ruta de un repartidor para un conjunto de pedidos, con memoria."""

    def __init__(self, matrix: Matrix) -> None:
        self._matrix = matrix
        self._memo: dict[tuple[int, frozenset[int]], float] = {}
        self.evaluations = 0

    def route_minutes(self, courier: int, orders: frozenset[int]) -> float:
        key = (courier, orders)
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        self.evaluations += 1
        nodes = [courier, *sorted(orders)]
        sub = [[self._matrix[a][b] for b in nodes] for a in nodes]
        # Matriz de ceros en la diagonal; ``None`` queda como sin conexión.
        for index in range(len(nodes)):
            sub[index][index] = 0.0
        if len(nodes) == 1:
            value = 0.0
        else:
            route = local_search(sub, nearest_neighbor(sub, 0))
            value = route_cost(sub, route)
        self._memo[key] = value
        return value


def estimate_cost(
    matrix: Matrix,
    courier_index: Mapping[str, int],
    order_index: Mapping[str, int],
    assignments: Mapping[str, Sequence[str]],
) -> AssignmentCost:
    evaluator = _Evaluator(matrix)
    return _cost_of(evaluator, courier_index, order_index, assignments)


def _cost_of(
    evaluator: _Evaluator,
    courier_index: Mapping[str, int],
    order_index: Mapping[str, int],
    assignments: Mapping[str, Sequence[str]],
) -> AssignmentCost:
    routes = [
        evaluator.route_minutes(courier_index[courier], frozenset(order_index[o] for o in orders))
        for courier, orders in assignments.items()
