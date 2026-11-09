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
    # Ir 0→1→2 es barato; el camino inverso por la matriz dirigida es carísimo.
    matrix: list[list[float | None]] = [[0, 1, 50], [50, 0, 1], [50, 50, 0]]
    assert held_karp_path(matrix) == [0, 1, 2]


def test_held_karp_no_confunde_sin_conexion_con_costo_cero() -> None:
    matrix: list[list[float | None]] = [[0, None, None], [None, 0, 1], [None, 1, 0]]
    assert held_karp_path(matrix) is None


def test_held_karp_evita_aristas_sin_conexion_si_hay_alternativa() -> None:
    matrix: list[list[float | None]] = [[0, 5, 1], [None, 0, 1], [1, 4, 0]]
    path = held_karp_path(matrix)
    assert path is not None and math.isfinite(route_cost(matrix, path))


def test_held_karp_rechaza_matrices_demasiado_grandes() -> None:
    with pytest.raises(ValueError):
        held_karp_path(random_matrix(MAX_EXACT_NODES + 1, random.Random(1)))


def test_busqueda_local_valida_y_nunca_empeora() -> None:
    rng = random.Random(11)
    for size in (6, 12, 25, 50):
        for _ in range(8):
            matrix = random_matrix(size, rng, holes=0.03)
            start = nearest_neighbor(matrix, 0)
            improved = local_search(matrix, start)
            assert is_valid(improved, size)
            assert route_cost(matrix, improved) <= route_cost(matrix, start) + 1e-9


def test_busqueda_local_iterada_es_determinista_y_no_empeora() -> None:
    rng = random.Random(5)
    matrix = random_matrix(30, rng)
    start = or_opt(matrix, two_opt(matrix, nearest_neighbor(matrix, 0)))
    first = iterated_local_search(matrix, start, kicks=20)
    second = iterated_local_search(matrix, start, kicks=20)
    assert first == second
    assert is_valid(first, 30)
    assert route_cost(matrix, first) <= route_cost(matrix, start) + 1e-9


def test_refinar_rutas_cortas_da_el_optimo_demostrable() -> None:
    rng = random.Random(21)
    for size in (5, 8, 10):
        for _ in range(10):
            matrix = random_matrix(size, rng)
            heuristic = or_opt(matrix, two_opt(matrix, nearest_neighbor(matrix, 0)))
            result = refine_route(matrix, heuristic)
            assert result.kind == "exact" and result.proven_optimal
            assert result.cost <= route_cost(matrix, heuristic) + 1e-9
            if size <= 8:
                assert result.cost == pytest.approx(brute_force(matrix))


def test_refinar_nunca_devuelve_algo_peor_y_conserva_el_origen() -> None:
    rng = random.Random(33)
    for size in (4, 12, 13, 24, 50):
        matrix = random_matrix(size, rng, holes=0.02)
        start = nearest_neighbor(matrix, 0)
        result = refine_route(matrix, start)
        assert is_valid(result.route, size)
        assert result.cost <= route_cost(matrix, start) + 1e-9


def test_refinar_cierra_la_brecha_de_la_heuristica_en_rutas_de_10_paradas() -> None:
    rng = random.Random(77)
    worse_before = 0
    for _ in range(60):
        matrix = random_matrix(10, rng)
        heuristic = or_opt(matrix, two_opt(matrix, nearest_neighbor(matrix, 0)))
        refined = refine_route(matrix, heuristic)
        assert refined.cost <= route_cost(matrix, heuristic) + 1e-9
        worse_before += route_cost(matrix, heuristic) > refined.cost + 1e-6
    assert worse_before >= 10  # la heurística sola deja mejoras reales sobre la mesa


def test_refinar_rutas_sin_conexion_no_inventa_un_camino() -> None:
    matrix: list[list[float | None]] = [
        [0, None, None, None],
        [None, 0, 1, 1],
        [None, 1, 0, 1],
        [None, 1, 1, 0],
    ]
    result = refine_route(matrix, [0, 1, 2, 3])
    assert result.route == [0, 1, 2, 3] or is_valid(result.route, 4)
    assert not math.isfinite(result.cost)


def test_cota_inferior_nunca_supera_al_optimo_y_usa_aristas_de_ambos_sentidos() -> None:
    from app.core.optimization.lower_bound import spanning_tree_lower_bound

    rng = random.Random(9)
    for size in (3, 5, 7):
        for _ in range(20):
            matrix = random_matrix(size, rng)
            bound = spanning_tree_lower_bound(matrix)
            assert bound is not None and bound <= brute_force(matrix) + 1e-9
    # Con una sola dirección disponible entre dos paradas, esa arista sí cuenta.
    one_way: list[list[float | None]] = [[0, 4, None], [None, 0, 2], [None, None, 0]]
    assert spanning_tree_lower_bound(one_way) == pytest.approx(6.0)


def test_cota_inferior_detecta_paradas_sin_conexion() -> None:
    from app.core.optimization.lower_bound import spanning_tree_lower_bound

    disconnected: list[list[float | None]] = [[0, 1, None], [1, 0, None], [None, None, 0]]
    assert spanning_tree_lower_bound(disconnected) is None


def test_refinar_largo_informa_su_cota_inferior() -> None:
    matrix = random_matrix(24, random.Random(2))
    result = refine_route(matrix, nearest_neighbor(matrix, 0))
    assert result.kind == "ils" and result.lower_bound is not None
    assert result.lower_bound <= result.cost
