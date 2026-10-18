"""Ordenación de una ruta abierta mediante vecino más cercano."""

from __future__ import annotations

import math
from collections.abc import Sequence

type CostMatrix = Sequence[Sequence[float | None]]


def nearest_neighbor(matrix: CostMatrix, start_index: int) -> list[int]:
    """Visita el siguiente punto de menor costo; null significa sin conexión."""
    _validate_matrix(matrix)
    if not matrix:
        return []
    if start_index < 0 or start_index >= len(matrix):
        raise IndexError("start_index está fuera de la matriz de costos")
    if len(matrix) == 1:
        return [start_index]

    route = [start_index]
    visited = {start_index}
    current = start_index

    while len(route) < len(matrix):
        nearest_index = -1
        nearest_cost = math.inf
        for candidate in range(len(matrix)):
            if candidate in visited:
                continue
            cost = matrix[current][candidate]
            candidate_cost = math.inf if cost is None else cost
            if nearest_index == -1 or candidate_cost < nearest_cost:
                nearest_index = candidate
                nearest_cost = candidate_cost

        if nearest_index == -1:
            break
        visited.add(nearest_index)
        route.append(nearest_index)
        current = nearest_index

    return route


def _validate_matrix(matrix: CostMatrix) -> None:
    size = len(matrix)
    for row in matrix:
        if len(row) != size:
            raise ValueError("La matriz de costos debe ser cuadrada")
        for cost in row:
            if cost is not None and (not math.isfinite(cost) or cost < 0):
                raise ValueError("Los costos deben ser null o números finitos no negativos")
