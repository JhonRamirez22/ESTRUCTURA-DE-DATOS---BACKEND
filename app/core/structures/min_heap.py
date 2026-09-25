"""Min-heap binario indexado por ID estable para Dijkstra y A*."""

from __future__ import annotations

from collections.abc import Callable, Hashable


class MinHeap[T, Id: Hashable]:
    """Heap mínimo con decrease_key en O(log n) y búsqueda de ID en O(1)."""

    def __init__(self, compare: Callable[[T, T], int], get_id: Callable[[T], Id]) -> None:
        self._items: list[T] = []
        self._indices: dict[Id, int] = {}
        self._compare = compare
        self._get_id = get_id

    def insert(self, item: T) -> None:
        item_id = self._get_id(item)
        if item_id in self._indices:
            raise ValueError(f"No se puede insertar un ID duplicado en el heap: {item_id!r}")

        self._items.append(item)
        index = len(self._items) - 1
        self._indices[item_id] = index
        self._bubble_up(index)

    def extract_min(self) -> T | None:
        if not self._items:
            return None

        minimum = self._items[0]
        minimum_id = self._get_id(minimum)
        last = self._items.pop()
        del self._indices[minimum_id]

        if self._items:
            self._items[0] = last
            self._indices[self._get_id(last)] = 0
            self._bubble_down(0)

        return minimum

    def decrease_key(self, item_id: Id, new_value: T) -> None:
        index = self._indices.get(item_id)
        if index is None:
            raise KeyError(f"El ID no existe en el heap: {item_id!r}")
        if self._get_id(new_value) != item_id:
            raise ValueError("decrease_key no puede reemplazar un elemento con otro ID")
