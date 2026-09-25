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
