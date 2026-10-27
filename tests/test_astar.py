from __future__ import annotations

import pytest

from app.core.graph.astar import astar
from app.core.graph.graph import Graph


def _build_graph() -> Graph[str]:
    graph: Graph[str] = Graph()
    graph.add_edge("a", "b", 1)
    graph.add_edge("b", "c", 1)
    graph.add_edge("c", "d", 1)
    graph.add_edge("a", "d", 10)
    graph.add_node("isolated")
    return graph


def test_astar_handles_empty_equal_and_unreachable_nodes() -> None:
    empty: Graph[str] = Graph()
    assert not astar(empty, "a", "b", lambda _left, _right: 0).found

    graph = _build_graph()
    same = astar(graph, "a", "a", lambda _left, _right: 0)
    assert same.found and same.path == ("a",) and same.total_cost == 0
    assert not astar(graph, "a", "isolated", lambda _left, _right: 0).found
