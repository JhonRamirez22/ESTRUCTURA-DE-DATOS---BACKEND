"""Mejora de ruta abierta con swaps 2-opt y primer punto fijo."""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.core.optimization.nearest_neighbor import CostMatrix, _validate_matrix

MAX_TWO_OPT_ITERATIONS = 1_000


def two_opt(matrix: CostMatrix, route: Sequence[int]) -> list[int]:
    """Mejora la ruta hasta converger o alcanzar el límite de iteraciones."""
    _validate_matrix(matrix)
    _validate_route(matrix, route)
    if len(route) < 4:
        return list(route)

    best_route = list(route)
    best_cost = _route_cost(matrix, best_route)
    improved = True
    iterations = 0

    while improved and iterations < MAX_TWO_OPT_ITERATIONS:
        improved = False
        iterations += 1
        # El repartidor permanece en el origen; las demás paradas, incluida la última, son libres.
        for start in range(1, len(best_route) - 1):
            for end in range(start + 1, len(best_route)):
                candidate = (
                    best_route[:start]
                    + list(reversed(best_route[start : end + 1]))
                    + best_route[end + 1 :]
                )
                candidate_cost = _route_cost(matrix, candidate)
                if candidate_cost < best_cost:
                    best_route = candidate
                    best_cost = candidate_cost
                    improved = True

    return best_route


def _route_cost(matrix: CostMatrix, route: Sequence[int]) -> float:
    total = 0.0
    for index in range(len(route) - 1):
        cost = matrix[route[index]][route[index + 1]]
        total += math.inf if cost is None else cost
    return total


def _validate_route(matrix: CostMatrix, route: Sequence[int]) -> None:
    seen: set[int] = set()
    for stop in route:
        if type(stop) is not int or stop < 0 or stop >= len(matrix):
            raise IndexError("La ruta contiene un índice fuera de la matriz de costos")
        if stop in seen:
            raise ValueError("La ruta no puede repetir índices de paradas")
        seen.add(stop)
