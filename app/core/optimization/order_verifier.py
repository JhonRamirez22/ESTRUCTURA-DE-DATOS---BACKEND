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


def effective_route_cost(graph: Graph[int], route: Sequence[int]) -> float:
    """Suma el camino mínimo entre paradas consecutivas; infinito si alguna no conecta."""
    total = 0.0
    for start, end in zip(route, route[1:], strict=False):
        result = dijkstra(graph, start, end)
        if not result.found:
            return math.inf
        total += result.total_cost
    return total


def is_valid_order(candidate: Sequence[int], reference: Sequence[int]) -> bool:
    """Debe visitar las mismas paradas una sola vez y conservar el origen fijo."""
    return (
        len(candidate) == len(reference)
        and len(set(candidate)) == len(candidate)
        and set(candidate) == set(reference)
        and bool(candidate)
        and candidate[0] == reference[0]
    )


def verify_candidate_order(
    matrix: Sequence[Sequence[float | None]],
    baseline: Sequence[int],
    candidate: Sequence[int],
    *,
    min_saving_minutes: float = MIN_SAVING_MINUTES,
) -> OrderVerification:
    if not is_valid_order(candidate, baseline):
        return OrderVerification(False, math.inf, math.inf, 0.0, "orden inválido")
    graph = build_cost_graph(matrix)
    baseline_cost = effective_route_cost(graph, baseline)
    candidate_cost = effective_route_cost(graph, candidate)
    if not math.isfinite(candidate_cost):
        return OrderVerification(False, baseline_cost, candidate_cost, 0.0, "sin camino conectado")
    saving = baseline_cost - candidate_cost
    if saving < min_saving_minutes:
        return OrderVerification(False, baseline_cost, candidate_cost, saving, "no mejora el costo")
    return OrderVerification(True, baseline_cost, candidate_cost, saving, "mejora verificada")
