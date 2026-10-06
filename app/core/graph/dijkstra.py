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
    heap.insert(_QueueEntry(id=source, cost=0.0))

    while not heap.is_empty():
        current = heap.extract_min()
        if current is None or current.id in settled:
            continue

        settled.add(current.id)
        if current.id == target:
            return build_path(source, target, previous, distances[target])

        for edge in graph.get_neighbors(current.id):
            if edge.to in settled:
                continue

            candidate_cost = current.cost + edge.weight
            if candidate_cost >= distances.get(edge.to, float("inf")):
                continue

            distances[edge.to] = candidate_cost
            previous[edge.to] = current.id
            entry = _QueueEntry(id=edge.to, cost=candidate_cost)
            if edge.to in queued:
                heap.decrease_key(edge.to, entry)
            else:
                heap.insert(entry)
                queued.add(edge.to)

    return no_path()
