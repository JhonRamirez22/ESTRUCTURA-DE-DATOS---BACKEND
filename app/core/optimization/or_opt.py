"""Or-opt para camino abierto: reubica tramos de 1 a 3 paradas consecutivas.

Complementa a 2-opt: 2-opt invierte tramos, Or-opt los traslada sin invertirlos. Así
corrige paradas "fuera de lugar" que 2-opt no alcanza con una sola inversión. El origen
(posición del repartidor) permanece fijo y la matriz se respeta como dirigida.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.core.optimization.nearest_neighbor import CostMatrix, _validate_matrix

MAX_OR_OPT_ITERATIONS = 1_000
MAX_SEGMENT_LENGTH = 3
# Una mejora menor que esto se considera ruido numérico y no reinicia la búsqueda.
_EPSILON = 1e-9


def or_opt(matrix: CostMatrix, route: Sequence[int]) -> list[int]:
    """Aplica la primera reubicación que mejora hasta converger o agotar iteraciones."""
    _validate_matrix(matrix)
    best_route = list(route)
    if len(best_route) < 3:
        return best_route
    best_cost = _route_cost(matrix, best_route)
    iterations = 0
    improved = True
    while improved and iterations < MAX_OR_OPT_ITERATIONS:
        improved = False
        iterations += 1
        for length in range(1, MAX_SEGMENT_LENGTH + 1):
            # Los tramos empiezan después del origen fijo.
            for start in range(1, len(best_route) - length + 1):
                segment = best_route[start : start + length]
                rest = best_route[:start] + best_route[start + length :]
                for insert_at in range(1, len(rest) + 1):
                    if insert_at == start:
                        continue  # reinsertar en el mismo lugar no cambia nada
                    candidate = rest[:insert_at] + segment + rest[insert_at:]
                    candidate_cost = _route_cost(matrix, candidate)
                    if candidate_cost < best_cost - _EPSILON:
                        best_route, best_cost = candidate, candidate_cost
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
    return best_route


def _route_cost(matrix: CostMatrix, route: Sequence[int]) -> float:
    total = 0.0
    for index in range(len(route) - 1):
        cost = matrix[route[index]][route[index + 1]]
        total += math.inf if cost is None else cost
    return total
