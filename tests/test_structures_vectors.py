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
