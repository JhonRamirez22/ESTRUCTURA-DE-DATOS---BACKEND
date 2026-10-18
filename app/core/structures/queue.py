"""Cola FIFO genérica respaldada por la lista enlazada simple del proyecto."""

from __future__ import annotations

from app.core.structures.singly_linked_list import SinglyLinkedList


class Queue[T]:
    """Inserta al final y extrae del frente en tiempo O(1)."""

    def __init__(self) -> None:
        self._items: SinglyLinkedList[T] = SinglyLinkedList()

    def enqueue(self, item: T) -> None:
        self._items.append(item)

    def dequeue(self) -> T | None:
        """Retorna y elimina el elemento más antiguo, o None si está vacía."""
        return self._items.pop_front()

    def peek(self) -> T | None:
        """Retorna el siguiente elemento sin eliminarlo, o None si está vacía."""
        return self._items.peek_front()

    def is_empty(self) -> bool:
        return len(self._items) == 0

    def __len__(self) -> int:
        return len(self._items)
