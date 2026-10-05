"""A* para grafos ponderados, con heurística en unidades del costo de arista."""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from app.core.graph.graph import Graph
from app.core.graph.path import PathResult, build_path, no_path
from app.core.structures.min_heap import MinHeap

# La heurística debe ser una cota inferior en las mismas unidades que Edge.weight.
type Heuristic[Id: Hashable] = Callable[[Id, Id], float]


@dataclass(frozen=True, slots=True)
class _OpenEntry[Id: Hashable]:
    id: Id
    g_cost: float
    f_cost: float


def astar[Id: Hashable](
    graph: Graph[Id],
    source: Id,
    target: Id,
    heuristic: Heuristic[Id],
) -> PathResult[Id]:
    """Calcula un camino mínimo si la heurística es admisible y consistente.

    La heurística debe ser una cota inferior no negativa en las mismas
    unidades que los pesos de las aristas. Nunca debe sobreestimarlos.
    """
    if not graph.has_node(source) or not graph.has_node(target):
        return no_path()

    def estimate(node: Id) -> float:
        value = heuristic(node, target)
        if not math.isfinite(value) or value < 0:
            raise ValueError("La heurística debe devolver un valor finito no negativo")
        return value

    scores: dict[Id, float] = {source: 0.0}
    previous: dict[Id, Id] = {}
    open_ids = {source}
    heap = MinHeap[_OpenEntry[Id], Id](
        compare=lambda left, right: (left.f_cost > right.f_cost) - (left.f_cost < right.f_cost),
        get_id=lambda entry: entry.id,
    )
    heap.insert(_OpenEntry(id=source, g_cost=0.0, f_cost=estimate(source)))

    while not heap.is_empty():
        current = heap.extract_min()
        if current is None:
            continue
        open_ids.discard(current.id)

        if current.id == target:
            return build_path(source, target, previous, scores[target])

        for edge in graph.get_neighbors(current.id):
            candidate_cost = current.g_cost + edge.weight
            if candidate_cost >= scores.get(edge.to, float("inf")):
                continue

            scores[edge.to] = candidate_cost
            previous[edge.to] = current.id
            entry = _OpenEntry(
                id=edge.to,
                g_cost=candidate_cost,
                f_cost=candidate_cost + estimate(edge.to),
            )
            if edge.to in open_ids:
                heap.decrease_key(edge.to, entry)
            else:
                heap.insert(entry)
                open_ids.add(edge.to)

    return no_path()
