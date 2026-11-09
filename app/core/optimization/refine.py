"""Refinamiento final del orden de visita: óptimo exacto o búsqueda local iterada.

NN + 2-opt + Or-opt dejan hasta ~10 % sobre el óptimo en rutas de 8 a 12 paradas (medido con
Held-Karp). Este módulo cierra esa brecha:

- Hasta ``MAX_EXACT_NODES`` nodos (origen incluido) se resuelve con programación dinámica sobre
  subconjuntos (Held-Karp): el resultado es el óptimo demostrable de la matriz dirigida.
- Con más nodos se aplica descenso por vecindarios (2-opt y Or-opt con costo de movimiento en
  O(1) mediante sumas prefijas) y se escapa de óptimos locales con *double-bridge* (búsqueda
  local iterada). Es determinista: la semilla sale del tamaño y del costo de la ruta.

El origen (posición del repartidor) permanece fijo y la matriz se respeta como dirigida. Un costo
``None`` significa «sin conexión», nunca costo cero.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.optimization.lower_bound import spanning_tree_lower_bound
from app.core.optimization.nearest_neighbor import CostMatrix, _validate_matrix

MAX_EXACT_NODES = 14
MAX_LOCAL_PASSES = 200
MAX_SEGMENT_LENGTH = 3
# Sustituye a «sin conexión» dentro de los cálculos incrementales (inf - inf daría NaN).
_UNREACHABLE = 1.0e9
_EPSILON = 1.0e-9

RefineKind = Literal["exact", "ils", "none"]


@dataclass(frozen=True, slots=True)
class RefineResult:
    route: list[int]
    cost: float
    kind: RefineKind
    #: Solo con ``kind == "exact"``: el costo es el mínimo posible para la matriz dada.
    proven_optimal: bool
    #: Árbol de expansión mínimo de la matriz: ningún orden puede costar menos que esto.
    lower_bound: float | None = None


def route_cost(matrix: CostMatrix, route: Sequence[int]) -> float:
    """Costo del camino abierto; infinito si alguna arista no tiene conexión."""
    total = 0.0
    for start, end in zip(route, route[1:], strict=False):
        cost = matrix[start][end]
        if cost is None:
            return math.inf
        total += cost
    return total


def held_karp_path(matrix: CostMatrix, start: int = 0) -> list[int] | None:
    """Camino abierto de costo mínimo desde ``start`` por todos los nodos; ``None`` si no existe."""
    _validate_matrix(matrix)
    size = len(matrix)
    if size == 0:
        return []
    if size == 1:
        return [start]
    if size > MAX_EXACT_NODES:
        raise ValueError(f"Held-Karp se limita a {MAX_EXACT_NODES} nodos")
    if not 0 <= start < size:
        raise IndexError("start está fuera de la matriz de costos")

    # Se renumera para que el origen sea el bit 0 y las máscaras sean compactas.
    others = [node for node in range(size) if node != start]
    order = [start, *others]
    cost = [
        [math.inf if matrix[a][b] is None else float(matrix[a][b] or 0.0) for b in order]
        for a in order
    ]
    full = (1 << size) - 1
    inf = math.inf
    dp = [[inf] * size for _ in range(1 << size)]
    parent = [[-1] * size for _ in range(1 << size)]
    dp[1][0] = 0.0
    for mask in range(1, 1 << size, 2):  # solo máscaras que contienen al origen
        row = dp[mask]
        for last in range(size):
            base = row[last]
            if base == inf:
                continue
            edge = cost[last]
            for nxt in range(1, size):
                bit = 1 << nxt
                if mask & bit:
                    continue
                candidate = base + edge[nxt]
                target = dp[mask | bit]
                if candidate < target[nxt]:
                    target[nxt] = candidate
                    parent[mask | bit][nxt] = last
    final = dp[full]
    last = min(range(1, size), key=final.__getitem__)
    if final[last] == inf:
        return None
    path = []
    mask = full
    while last != -1:
        path.append(order[last])
        previous = parent[mask][last]
        mask ^= 1 << last
        last = previous
    path.reverse()
    return path


def _finite(matrix: CostMatrix) -> list[list[float]]:
    return [[_UNREACHABLE if cost is None else float(cost) for cost in row] for row in matrix]


def local_search(matrix: CostMatrix, route: Sequence[int]) -> list[int]:
    """2-opt y Or-opt alternados hasta que ninguno mejora, con costo de movimiento en O(1)."""
    _validate_matrix(matrix)
    return _descend(_finite(matrix), list(route))


def _descend(cost: list[list[float]], route: list[int]) -> list[int]:
    for _ in range(MAX_LOCAL_PASSES):
        if _two_opt_pass(cost, route) or _or_opt_pass(cost, route):
            continue
        break
    return route


def _two_opt_pass(cost: list[list[float]], route: list[int]) -> bool:
    """Primera inversión que mejora. Las sumas prefijas dan el costo del tramo invertido en O(1)."""
    size = len(route)
    if size < 4:
        return False
    forward = [0.0] * size
    backward = [0.0] * size
    for index in range(size - 1):
        forward[index + 1] = forward[index] + cost[route[index]][route[index + 1]]
        backward[index + 1] = backward[index] + cost[route[index + 1]][route[index]]
    for start in range(1, size - 1):
        before = route[start - 1]
        first = route[start]
        for end in range(start + 1, size):
            last = route[end]
            after = route[end + 1] if end + 1 < size else -1
            old = cost[before][first] + (forward[end] - forward[start])
            new = cost[before][last] + (backward[end] - backward[start])
            if after != -1:
                old += cost[last][after]
                new += cost[first][after]
            if new < old - _EPSILON:
                route[start : end + 1] = route[start : end + 1][::-1]
                return True
    return False


def _or_opt_pass(cost: list[list[float]], route: list[int]) -> bool:
    """Primer traslado de un tramo de 1 a 3 paradas (sin invertirlo) que mejora."""
    size = len(route)
    if size < 4:
        return False
    for length in range(1, MAX_SEGMENT_LENGTH + 1):
        for start in range(1, size - length + 1):
            stop = start + length - 1
            head, tail = route[start], route[stop]
            previous = route[start - 1]
            following = route[stop + 1] if stop + 1 < size else -1
            removed = cost[previous][head]
            if following != -1:
                removed += cost[tail][following] - cost[previous][following]
            # Aristas (k, k+1) de la ruta original que no tocan el tramo.
            for edge in range(size):
                if start - 1 <= edge <= stop:
                    continue
                source = route[edge]
                target = route[edge + 1] if edge + 1 < size else -1
                added = cost[source][head]
                if target != -1:
                    added += cost[tail][target] - cost[source][target]
                if added < removed - _EPSILON:
                    segment = route[start : stop + 1]
                    rest = route[:start] + route[stop + 1 :]
                    position = edge + 1 if edge < start else edge + 1 - length
                    route[:] = rest[:position] + segment + rest[position:]
                    return True
    return False


def _double_bridge(route: list[int], rng: random.Random) -> list[int]:
    """A B C D → A C B D: reordena tramos sin invertirlos, adecuado para matrices dirigidas."""
    size = len(route)
    cuts = sorted(rng.sample(range(1, size), 3))
    first, second, third = cuts
    return route[:first] + route[second:third] + route[first:second] + route[third:]


def iterated_local_search(
    matrix: CostMatrix, route: Sequence[int], *, kicks: int, seed: int | None = None
) -> list[int]:
    """Búsqueda local iterada: perturba con double-bridge y conserva solo mejoras."""
    _validate_matrix(matrix)
    cost = _finite(matrix)
    best = _descend(cost, list(route))
    if len(best) < 8 or kicks <= 0:
        return best
    best_cost = _total(cost, best)
    rng = random.Random(seed if seed is not None else len(best) * 7919 + int(best_cost * 100))
    for _ in range(kicks):
        candidate = _descend(cost, _double_bridge(best, rng))
        candidate_cost = _total(cost, candidate)
        if candidate_cost < best_cost - _EPSILON:
            best, best_cost = candidate, candidate_cost
    return best


def _total(cost: list[list[float]], route: Sequence[int]) -> float:
    return sum(cost[a][b] for a, b in zip(route, route[1:], strict=False))


def _kicks_for(size: int) -> int:
    """Menos reinicios cuanto más grande es la ruta, para acotar el tiempo (≈0,3 s a 50 paradas)."""
    if size <= 20:
        return 80
    if size <= 35:
        return 50
    return 30


def refine_route(matrix: CostMatrix, route: Sequence[int]) -> RefineResult:
    """Mejor orden conocido: óptimo exacto si cabe, búsqueda local iterada si no.

    Nunca devuelve una ruta peor que la recibida ni cambia el origen.
    """
    _validate_matrix(matrix)
    current = list(route)
    current_cost = route_cost(matrix, current)
    if len(current) < 3:
        return RefineResult(current, current_cost, "none", proven_optimal=len(current) < 3)
    origin = current[0]
    if len(matrix) == len(current) and len(current) <= MAX_EXACT_NODES:
        exact = held_karp_path(matrix, origin)
        if exact is not None:
            exact_cost = route_cost(matrix, exact)
            if exact_cost <= current_cost:
                return RefineResult(exact, exact_cost, "exact", proven_optimal=True)
            return RefineResult(current, current_cost, "exact", proven_optimal=True)
    bound = spanning_tree_lower_bound(matrix)
    improved = iterated_local_search(matrix, current, kicks=_kicks_for(len(current)))
    improved_cost = route_cost(matrix, improved)
    if improved_cost < current_cost and improved[0] == origin:
        return RefineResult(improved, improved_cost, "ils", proven_optimal=False, lower_bound=bound)
    return RefineResult(current, current_cost, "ils", proven_optimal=False, lower_bound=bound)
