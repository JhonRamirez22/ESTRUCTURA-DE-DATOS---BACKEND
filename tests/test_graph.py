from __future__ import annotations

import math

import pytest

from app.core.graph.graph import Edge, Graph


def test_graph_starts_empty_and_returns_no_neighbors_for_unknown_node() -> None:
    graph: Graph[str] = Graph()
    assert len(graph) == 0
    assert not graph.has_node("unknown")
    assert graph.get_neighbors("unknown") == ()


def test_graph_stores_weighted_directed_edges() -> None:
    graph: Graph[str] = Graph()
    graph.add_edge("a", "b", 4.5)

    assert len(graph) == 2
    assert graph.has_node("a")
    assert graph.has_node("b")
    assert graph.get_neighbors("a") == (Edge(to="b", weight=4.5),)
    assert graph.get_neighbors("b") == ()


@pytest.mark.parametrize("weight", [-1.0, math.inf, math.nan])
def test_graph_rejects_negative_or_non_finite_weights(weight: float) -> None:
    graph: Graph[str] = Graph()
    with pytest.raises(ValueError, match="finito y no negativo"):
        graph.add_edge("a", "b", weight)
