"""Refinamiento: óptimo exacto (Held-Karp), búsqueda local y búsqueda iterada."""

import itertools
import math
import random

import pytest

from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.or_opt import or_opt
from app.core.optimization.refine import (
    MAX_EXACT_NODES,
    held_karp_path,
    iterated_local_search,
    local_search,
    refine_route,
    route_cost,
)
from app.core.optimization.two_opt import two_opt


def random_matrix(size: int, rng: random.Random, holes: float = 0.0) -> list[list[float | None]]:
    points = [(rng.random() * 4000, rng.random() * 3000) for _ in range(size)]
    matrix: list[list[float | None]] = [[0.0] * size for _ in range(size)]
    for source in range(size):
        for target in range(size):
            if source == target:
                continue
            dx = abs(points[source][0] - points[target][0])
            dy = abs(points[source][1] - points[target][1])
            distance = dx + dy
            factor = rng.uniform(0.85, 1.25) * rng.choice([1, 1, 1, 1.6])
            matrix[source][target] = None if rng.random() < holes else distance / 400 * factor
    return matrix


def brute_force(matrix: list[list[float | None]]) -> float:
    best = math.inf
    for rest in itertools.permutations(range(1, len(matrix))):
        best = min(best, route_cost(matrix, [0, *rest]))
    return best


def is_valid(route: list[int], size: int) -> bool:
    return route[0] == 0 and sorted(route) == list(range(size))


@pytest.mark.parametrize("size", [2, 3, 4, 5, 6, 7])
def test_held_karp_iguala_la_fuerza_bruta(size: int) -> None:
    rng = random.Random(size)
    for _ in range(25):
        matrix = random_matrix(size, rng)
        path = held_karp_path(matrix)
        assert path is not None and is_valid(path, size)
        assert route_cost(matrix, path) == pytest.approx(brute_force(matrix))


def test_held_karp_respeta_el_sentido_de_las_aristas() -> None:
