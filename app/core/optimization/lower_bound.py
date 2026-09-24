"""Cota inferior del costo de una ruta: árbol de expansión mínimo (Kruskal).

Todo camino que visita cada parada una vez es un árbol de expansión, y cada arista dirigida
cuesta al menos lo que la más barata de sus dos direcciones. Por eso el árbol de expansión
mínimo de la matriz simetrizada nunca supera al óptimo. Sirve para certificar cuánto se puede
alejar una ruta heurística del mejor orden posible cuando Held-Karp ya no cabe.

Usa las estructuras propias: ``MinHeap`` ordena las aristas y ``DisjointSet`` evita ciclos.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.optimization.nearest_neighbor import CostMatrix, _validate_matrix
from app.core.structures.disjoint_set import DisjointSet
from app.core.structures.min_heap import MinHeap


@dataclass(frozen=True, slots=True)
class _Edge:
    weight: float
    left: int
    right: int


def _compare(left: _Edge, right: _Edge) -> int:
