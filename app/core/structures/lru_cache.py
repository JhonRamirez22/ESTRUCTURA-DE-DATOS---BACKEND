"""Caché LRU acotada: diccionario para el acceso O(1) y lista doble para el orden de uso."""

from __future__ import annotations

from collections.abc import Hashable

from app.core.structures.doubly_linked_list import DoublyLinkedList, DoublyLinkedNode


class LruCache[K: Hashable, V]:
    """Conserva los `capacity` elementos usados más recientemente.

    El frente de la lista es el más reciente; al superar la capacidad se descarta el del
    final. Leer, escribir y descartar son O(1) porque el diccionario guarda el nodo.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("La capacidad de la caché debe ser al menos 1.")
        self._capacity = capacity
        self._order: DoublyLinkedList[tuple[K, V]] = DoublyLinkedList()
        self._nodes: dict[K, DoublyLinkedNode[tuple[K, V]]] = {}

    def get(self, key: K) -> V | None:
        node = self._nodes.get(key)
        if node is None:
            return None
        self._order.move_to_front(node)
        return node.value[1]

    def put(self, key: K, value: V) -> None:
        existing = self._nodes.get(key)
        if existing is not None:
            self._order.remove(existing)
        self._nodes[key] = self._order.appendleft((key, value))
        while len(self._nodes) > self._capacity:
            evicted = self._order.pop_back()
            if evicted is None:
                break
            self._nodes.pop(evicted[0], None)

    def __contains__(self, key: object) -> bool:
        return key in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def clear(self) -> None:
        self._order = DoublyLinkedList()
        self._nodes.clear()
