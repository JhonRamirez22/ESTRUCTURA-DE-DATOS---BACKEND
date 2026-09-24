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
