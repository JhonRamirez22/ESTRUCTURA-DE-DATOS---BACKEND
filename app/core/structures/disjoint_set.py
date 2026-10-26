"""Unión-find (conjuntos disjuntos) con compresión de caminos y unión por tamaño.

Agrupa elementos que se conectan entre sí. Cada operación cuesta casi O(1) amortizado
(inversa de Ackermann), así que agrupar n pedidos con k vecindades es O(k α(n)).
"""

from __future__ import annotations

from collections.abc import Hashable


class DisjointSet[T: Hashable]:
    def __init__(self, items: list[T]) -> None:
        self._parent: dict[T, T] = {item: item for item in items}
        self._size: dict[T, int] = {item: 1 for item in items}

    def find(self, item: T) -> T:
        # Compresión por mitad: cada salto apunta al abuelo.
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: T, right: T) -> bool:
        """Une los conjuntos de `left` y `right`; devuelve False si ya estaban unidos."""
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return False
        if self._size[root_left] < self._size[root_right]:
            root_left, root_right = root_right, root_left
        self._parent[root_right] = root_left
        self._size[root_left] += self._size[root_right]
        return True

    def size_of(self, item: T) -> int:
        return self._size[self.find(item)]

    def groups(self) -> list[list[T]]:
        buckets: dict[T, list[T]] = {}
        for item in self._parent:
            buckets.setdefault(self.find(item), []).append(item)
        return list(buckets.values())
