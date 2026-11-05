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


def test_astar_finds_minimum_path_using_admissible_time_heuristic() -> None:
    graph: Graph[str] = Graph()
    graph.add_edge("a", "b", 5)
    graph.add_edge("b", "c", 8)
    graph.add_edge("c", "d", 12)
    graph.add_edge("a", "d", 40)
    order = {"a": 0, "b": 1, "c": 2, "d": 3}
    minutes_per_coordinate_unit = 4.0

    def time_heuristic(source: str, target: str) -> float:
        return abs(order[target] - order[source]) * minutes_per_coordinate_unit

    result = astar(graph, "a", "d", time_heuristic)
    assert result.found
    assert result.path == ("a", "b", "c", "d")
    assert result.total_cost == 25


@pytest.mark.parametrize("estimate", [-1.0, float("inf"), float("nan")])
def test_astar_rejects_invalid_heuristic_values(estimate: float) -> None:
    graph = _build_graph()
    with pytest.raises(ValueError, match="finito no negativo"):
        astar(graph, "a", "d", lambda _source, _target: estimate)
