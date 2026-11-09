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
    return (left.weight > right.weight) - (left.weight < right.weight)


def spanning_tree_lower_bound(matrix: CostMatrix) -> float | None:
    """Peso del árbol de expansión mínimo; ``None`` si alguna parada no se puede conectar."""
    _validate_matrix(matrix)
    size = len(matrix)
    if size < 2:
        return 0.0
    heap = MinHeap[_Edge, tuple[int, int]](_compare, lambda edge: (edge.left, edge.right))
    for left in range(size):
        for right in range(left + 1, size):
            forward, backward = matrix[left][right], matrix[right][left]
            options = [cost for cost in (forward, backward) if cost is not None]
            if options:
                heap.insert(_Edge(min(options), left, right))
    components = DisjointSet(list(range(size)))
    total = 0.0
    joined = 0
    while joined < size - 1:
        edge = heap.extract_min()
        if edge is None:
            return None
        if components.union(edge.left, edge.right):
            total += edge.weight
            joined += 1
    return total
