"""Lista enlazada simple con inserción en extremos y extracción FIFO."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(slots=True)
class _Node[T]:
    value: T
    next: _Node[T] | None = None


class SinglyLinkedList[T]:
    """Lista O(1) en extremos; remove busca el primer valor en O(n)."""

    def __init__(self) -> None:
        self._head: _Node[T] | None = None
        self._tail: _Node[T] | None = None
        self._size = 0

    def append(self, value: T) -> None:
        node = _Node(value)
        if self._tail is None:
            self._head = node
        else:
            self._tail.next = node
        self._tail = node
        self._size += 1

    def prepend(self, value: T) -> None:
        node = _Node(value, self._head)
        self._head = node
        if self._tail is None:
            self._tail = node
        self._size += 1

    def pop_front(self) -> T | None:
        if self._head is None:
            return None
        node = self._head
        self._head = node.next
        node.next = None
