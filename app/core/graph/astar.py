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
