"""Ejecuta los vectores compartidos con el frontend TypeScript (src/core/structures)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.structures.min_heap import MinHeap
from app.core.structures.queue import Queue
from app.core.structures.singly_linked_list import SinglyLinkedList
from app.core.structures.stack import Stack

VECTORS_PATH = Path(__file__).parent / "fixtures" / "structures-vectors.json"
CASES: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["cases"]


def _create(structure: str) -> Any:
    if structure == "queue":
        return Queue[Any]()
    if structure == "stack":
        return Stack[Any]()
    if structure == "singly_linked_list":
        return SinglyLinkedList[Any]()
    if structure == "min_heap":
        return MinHeap[dict[str, Any], str](
            compare=lambda left, right: left["priority"] - right["priority"],
            get_id=lambda item: item["id"],
        )
    raise AssertionError(f"Estructura desconocida en vectores: {structure}")


def _call(target: Any, call: str, args: list[Any]) -> Any:
    if call == "size":
        return len(target)
    if call == "to_array":
        return list(target)
    return getattr(target, call)(*args)


def _expects_error(expected: Any) -> bool:
    return isinstance(expected, dict) and expected.get("throws") is True


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_vectors_match_typescript_behavior(case: dict[str, Any]) -> None:
    target = _create(case["structure"])
    for operation in case["ops"]:
        args = operation.get("args", [])
        expected = operation["expect"]
        if _expects_error(expected):
            with pytest.raises((ValueError, KeyError)):
                _call(target, operation["call"], args)
        else:
            assert _call(target, operation["call"], args) == expected, operation["call"]
