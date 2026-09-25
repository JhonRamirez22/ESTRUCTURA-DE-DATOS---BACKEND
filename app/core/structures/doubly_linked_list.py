"""Lista doble que permite retirar o mover un nodo conocido en O(1)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(slots=True, eq=False)
class DoublyLinkedNode[T]:
    value: T
    previous: DoublyLinkedNode[T] | None = None
    next: DoublyLinkedNode[T] | None = None
    _owner: object | None = None


class DoublyLinkedList[T]:
    """Lista con acceso O(1) a ambos extremos y a nodos retenidos por el caller."""

    def __init__(self) -> None:
        self._head: DoublyLinkedNode[T] | None = None
        self._tail: DoublyLinkedNode[T] | None = None
        self._size = 0
        self._owner = object()

    def append(self, value: T) -> DoublyLinkedNode[T]:
        node = DoublyLinkedNode(value, previous=self._tail, _owner=self._owner)
        if self._tail is None:
            self._head = node
        else:
            self._tail.next = node
        self._tail = node
        self._size += 1
        return node

    def appendleft(self, value: T) -> DoublyLinkedNode[T]:
        node = DoublyLinkedNode(value, next=self._head, _owner=self._owner)
        if self._head is None:
            self._tail = node
        else:
            self._head.previous = node
        self._head = node
        self._size += 1
        return node

    def remove(self, node: DoublyLinkedNode[T]) -> T:
        if node._owner is not self._owner:
            raise ValueError("node is not attached to this list")
