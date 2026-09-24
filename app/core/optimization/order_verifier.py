"""Verifica con Dijkstra si un orden propuesto por la IA realmente cuesta menos.

La IA solo sugiere; este módulo recalcula el costo con la misma matriz y el grafo dirigido
de paradas. Usar el camino mínimo (y no solo la arista directa) respeta que ir de A a B por
una parada intermedia puede ser más rápido que el tramo directo cuando la matriz tiene
tráfico ajustado.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.graph.dijkstra import dijkstra
from app.core.graph.graph import Graph

MIN_SAVING_MINUTES = 0.5


@dataclass(frozen=True, slots=True)
class OrderVerification:
    accepted: bool
    baseline_cost: float
    candidate_cost: float
    saving_minutes: float
    reason: str


def build_cost_graph(matrix: Sequence[Sequence[float | None]]) -> Graph[int]:
    graph = Graph[int]()
    for index in range(len(matrix)):
        graph.add_node(index)
    for source, row in enumerate(matrix):
        for target, cost in enumerate(row):
            if source != target and cost is not None and math.isfinite(cost) and cost >= 0:
                graph.add_edge(source, target, float(cost))
    return graph


