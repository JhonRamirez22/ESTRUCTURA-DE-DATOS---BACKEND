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
