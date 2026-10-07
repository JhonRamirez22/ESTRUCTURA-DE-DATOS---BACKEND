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
        self._size -= 1
        if self._head is None:
            self._tail = None
        return node.value

    def remove(self, value: T) -> bool:
        previous: _Node[T] | None = None
        current = self._head
        while current is not None:
            if current.value == value:
                if previous is None:
                    self._head = current.next
                else:
                    previous.next = current.next
                if current is self._tail:
                    self._tail = previous
                current.next = None
                self._size -= 1
                return True
            previous, current = current, current.next
        return False

    def peek_front(self) -> T | None:
        return None if self._head is None else self._head.value

    def clear(self) -> None:
        self._head = None
        self._tail = None
        self._size = 0

    def __contains__(self, value: object) -> bool:
        return any(item == value for item in self)

    def __iter__(self) -> Iterator[T]:
        current = self._head
        while current is not None:
            yield current.value
            current = current.next

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return self._size > 0
