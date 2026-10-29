from __future__ import annotations

import math

from app.core.graph.dijkstra import dijkstra
from app.core.graph.graph import Graph


def _build_graph() -> Graph[str]:
    graph: Graph[str] = Graph()
    graph.add_edge("a", "b", 1)
    graph.add_edge("b", "c", 1)
    graph.add_edge("c", "d", 1)
    graph.add_edge("a", "d", 10)
    graph.add_node("isolated")
    return graph


def test_dijkstra_returns_no_path_for_empty_or_disconnected_graph() -> None:
    empty: Graph[str] = Graph()
    assert dijkstra(empty, "a", "b").found is False

    result = dijkstra(_build_graph(), "a", "isolated")
    assert result.found is False
    assert result.path == ()
