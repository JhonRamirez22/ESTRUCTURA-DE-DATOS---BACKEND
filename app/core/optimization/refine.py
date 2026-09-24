"""Refinamiento final del orden de visita: óptimo exacto o búsqueda local iterada.

NN + 2-opt + Or-opt dejan hasta ~10 % sobre el óptimo en rutas de 8 a 12 paradas (medido con
Held-Karp). Este módulo cierra esa brecha:

- Hasta ``MAX_EXACT_NODES`` nodos (origen incluido) se resuelve con programación dinámica sobre
  subconjuntos (Held-Karp): el resultado es el óptimo demostrable de la matriz dirigida.
- Con más nodos se aplica descenso por vecindarios (2-opt y Or-opt con costo de movimiento en
  O(1) mediante sumas prefijas) y se escapa de óptimos locales con *double-bridge* (búsqueda
  local iterada). Es determinista: la semilla sale del tamaño y del costo de la ruta.

El origen (posición del repartidor) permanece fijo y la matriz se respeta como dirigida. Un costo
``None`` significa «sin conexión», nunca costo cero.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.optimization.lower_bound import spanning_tree_lower_bound
from app.core.optimization.nearest_neighbor import CostMatrix, _validate_matrix

MAX_EXACT_NODES = 14
MAX_LOCAL_PASSES = 200
MAX_SEGMENT_LENGTH = 3
# Sustituye a «sin conexión» dentro de los cálculos incrementales (inf - inf daría NaN).
_UNREACHABLE = 1.0e9
_EPSILON = 1.0e-9

RefineKind = Literal["exact", "ils", "none"]


@dataclass(frozen=True, slots=True)
class RefineResult:
    route: list[int]
    cost: float
    kind: RefineKind
    #: Solo con ``kind == "exact"``: el costo es el mínimo posible para la matriz dada.
    proven_optimal: bool
    #: Árbol de expansión mínimo de la matriz: ningún orden puede costar menos que esto.
    lower_bound: float | None = None


def route_cost(matrix: CostMatrix, route: Sequence[int]) -> float:
    """Costo del camino abierto; infinito si alguna arista no tiene conexión."""
    total = 0.0
    for start, end in zip(route, route[1:], strict=False):
        cost = matrix[start][end]
        if cost is None:
            return math.inf
        total += cost
    return total


def held_karp_path(matrix: CostMatrix, start: int = 0) -> list[int] | None:
    """Camino abierto de costo mínimo desde ``start`` por todos los nodos; ``None`` si no existe."""
    _validate_matrix(matrix)
    size = len(matrix)
    if size == 0:
        return []
