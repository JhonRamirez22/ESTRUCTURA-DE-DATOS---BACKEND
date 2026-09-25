"""Pila LIFO genérica implementada con operaciones O(1) en la cabeza."""

from __future__ import annotations

from app.core.structures.singly_linked_list import SinglyLinkedList


class Stack[T]:
    """Apila y desapila al frente para evitar desplazar elementos."""

    def __init__(self) -> None:
        self._items: SinglyLinkedList[T] = SinglyLinkedList()

    def push(self, item: T) -> None:
        self._items.prepend(item)

    def pop(self) -> T | None:
        """Retorna y elimina el elemento superior, o None si está vacía."""
        return self._items.pop_front()

    def peek(self) -> T | None:
        """Retorna el elemento superior sin eliminarlo, o None si está vacía."""
        return self._items.peek_front()

    def is_empty(self) -> bool:
        return len(self._items) == 0

    def __len__(self) -> int:
        return len(self._items)
