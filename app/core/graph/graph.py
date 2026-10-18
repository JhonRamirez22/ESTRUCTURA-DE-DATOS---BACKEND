"""Grafo dirigido ponderado mediante lista de adyacencia."""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Edge[Id: Hashable]:
    to: Id
    weight: float


class Graph[Id: Hashable]:
    """Almacena solo las aristas salientes; no crea la inversa automáticamente."""

    def __init__(self) -> None:
        self._adjacency: dict[Id, list[Edge[Id]]] = {}

    def add_node(self, node_id: Id) -> None:
        self._adjacency.setdefault(node_id, [])

    def add_edge(self, source: Id, target: Id, weight: float) -> None:
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("El peso de una arista debe ser finito y no negativo")

        self.add_node(source)
        self.add_node(target)
        self._adjacency[source].append(Edge(target, weight))

    def get_neighbors(self, node_id: Id) -> tuple[Edge[Id], ...]:
        return tuple(self._adjacency.get(node_id, ()))

    def has_node(self, node_id: Id) -> bool:
        return node_id in self._adjacency

    def __len__(self) -> int:
        return len(self._adjacency)
