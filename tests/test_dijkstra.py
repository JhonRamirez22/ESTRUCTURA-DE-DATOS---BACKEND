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
    assert result.total_cost == math.inf


def test_dijkstra_returns_zero_cost_for_same_source_and_target() -> None:
    graph: Graph[str] = Graph()
    graph.add_node("a")

    assert dijkstra(graph, "a", "a").path == ("a",)
    assert dijkstra(graph, "a", "a").total_cost == 0


def test_dijkstra_chooses_cheapest_path_not_fewest_edges() -> None:
    result = dijkstra(_build_graph(), "a", "d")
    assert result.found is True
    assert result.path == ("a", "b", "c", "d")
    assert result.total_cost == 3


def test_dijkstra_respects_one_way_edges() -> None:
    graph: Graph[str] = Graph()
    graph.add_edge("east", "west", 2)

    assert dijkstra(graph, "east", "west").found
    assert not dijkstra(graph, "west", "east").found
