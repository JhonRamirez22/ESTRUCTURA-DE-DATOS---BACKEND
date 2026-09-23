"""Camino mínimo de Dijkstra con min-heap indexado."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass

from app.core.graph.graph import Graph
from app.core.graph.path import PathResult, build_path, no_path
from app.core.structures.min_heap import MinHeap


@dataclass(frozen=True, slots=True)
class _QueueEntry[Id: Hashable]:
    id: Id
    cost: float


def dijkstra[Id: Hashable](graph: Graph[Id], source: Id, target: Id) -> PathResult[Id]:
    if not graph.has_node(source) or not graph.has_node(target):
        return no_path()

    distances: dict[Id, float] = {source: 0.0}
    previous: dict[Id, Id] = {}
    queued = {source}
    settled: set[Id] = set()
    heap = MinHeap[_QueueEntry[Id], Id](
        compare=lambda left, right: (left.cost > right.cost) - (left.cost < right.cost),
        get_id=lambda entry: entry.id,
    )
