from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.structures.doubly_linked_list import DoublyLinkedList
from app.core.structures.min_heap import MinHeap
from app.core.structures.queue import Queue
from app.core.structures.singly_linked_list import SinglyLinkedList
from app.core.structures.stack import Stack


def test_singly_linked_list_handles_empty_single_and_removal() -> None:
    items: SinglyLinkedList[int] = SinglyLinkedList()
    assert items.pop_front() is None
    assert items.peek_front() is None

    items.append(2)
    assert items.remove(2)
    assert not items

    items.prepend(1)
    items.append(3)
    assert items.remove(1)
    assert items.remove(3)
    assert list(items) == []


def test_singly_linked_list_preserves_order_and_reports_missing_value() -> None:
    items: SinglyLinkedList[str] = SinglyLinkedList()
    items.append("b")
    items.prepend("a")
    items.append("c")

    assert list(items) == ["a", "b", "c"]
    assert len(items) == 3
    assert not items.remove("missing")
    assert items.pop_front() == "a"
    assert items.pop_front() == "b"
    assert items.pop_front() == "c"
    assert not items


def test_doubly_linked_list_removes_and_moves_nodes_in_constant_time() -> None:
    items: DoublyLinkedList[str] = DoublyLinkedList()
    assert items.pop_front() is None
    assert items.pop_back() is None

    first = items.append("first")
    middle = items.append("middle")
    last = items.append("last")
    items.move_to_front(last)
    assert list(items) == ["last", "first", "middle"]
    assert items.remove(first) == "first"
    assert items.pop_front() == "last"
    assert items.pop_back() == "middle"
    assert not items

    with pytest.raises(ValueError, match="not attached"):
        items.remove(middle)


def test_doubly_linked_list_rejects_nodes_from_another_list() -> None:
    first_list: DoublyLinkedList[int] = DoublyLinkedList()
    second_list: DoublyLinkedList[int] = DoublyLinkedList()
    foreign_node = first_list.append(7)

    with pytest.raises(ValueError, match="not attached"):
        second_list.move_to_front(foreign_node)


def test_queue_supports_fifo_empty_and_single_item() -> None:
    queue: Queue[int] = Queue()
    assert queue.is_empty()
    assert queue.peek() is None
    assert queue.dequeue() is None

    queue.enqueue(1)
    assert queue.peek() == 1
    assert queue.dequeue() == 1
    assert queue.is_empty()

    queue.enqueue(2)
    queue.enqueue(3)
    assert len(queue) == 2
    assert [queue.dequeue(), queue.dequeue()] == [2, 3]


def test_stack_supports_lifo_empty_and_single_item() -> None:
    stack: Stack[int] = Stack()
    assert stack.is_empty()
    assert stack.peek() is None
    assert stack.pop() is None

    stack.push(1)
    assert stack.peek() == 1
    assert stack.pop() == 1
    assert stack.is_empty()

    stack.push(2)
    stack.push(3)
    assert len(stack) == 2
    assert [stack.pop(), stack.pop()] == [3, 2]


@dataclass(frozen=True)
class _PriorityItem:
    id: str
    priority: int


def _heap() -> MinHeap[_PriorityItem, str]:
    return MinHeap(
        compare=lambda left, right: left.priority - right.priority,
        get_id=lambda item: item.id,
    )


def test_min_heap_extracts_in_priority_order_and_handles_empty_and_single() -> None:
    heap = _heap()
    assert heap.extract_min() is None
    assert heap.is_empty()

    heap.insert(_PriorityItem("only", 8))
    assert heap.extract_min() == _PriorityItem("only", 8)
    assert heap.extract_min() is None

    heap.insert(_PriorityItem("three", 3))
    heap.insert(_PriorityItem("one", 1))
    heap.insert(_PriorityItem("two", 2))
    assert [heap.extract_min().priority for _ in range(3)] == [1, 2, 3]


def test_min_heap_tracks_ids_after_swaps_and_extracts() -> None:
    heap = _heap()
    for item in (
        _PriorityItem("a", 10),
        _PriorityItem("b", 4),
        _PriorityItem("c", 8),
        _PriorityItem("d", 2),
        _PriorityItem("e", 6),
    ):
        heap.insert(item)

    assert heap.extract_min() == _PriorityItem("d", 2)
    assert heap.extract_min() == _PriorityItem("b", 4)
    heap.decrease_key("a", _PriorityItem("a", 1))
    assert heap.extract_min() == _PriorityItem("a", 1)
    assert heap.extract_min() == _PriorityItem("e", 6)
    assert heap.extract_min() == _PriorityItem("c", 8)
    assert heap.is_empty()


def test_min_heap_rejects_invalid_decrease_and_duplicate_ids() -> None:
    heap = _heap()
    heap.insert(_PriorityItem("known", 3))

    with pytest.raises(KeyError, match="no existe"):
        heap.decrease_key("missing", _PriorityItem("missing", 1))
    with pytest.raises(ValueError, match="aumentar"):
        heap.decrease_key("known", _PriorityItem("known", 5))
    with pytest.raises(ValueError, match="ID duplicado"):
        heap.insert(_PriorityItem("known", 1))
    with pytest.raises(ValueError, match="otro ID"):
        heap.decrease_key("known", _PriorityItem("other", 1))
