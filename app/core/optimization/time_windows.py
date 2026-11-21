"""Orden de visita con ventanas horarias y prioridad (VRPTW de un solo repartidor).

Tiempos en minutos desde el inicio de la ruta; el nodo 0 es el repartidor y se queda fijo.
Una parada puede tener ``earliest`` (si llega antes, espera) y ``latest`` (si llega después, hay
retraso). El objetivo, en orden de importancia, es:

    1. minutos de retraso ponderados por la prioridad de cada parada;
    2. minutos totales hasta terminar la ruta.

Se parte de dos semillas (el orden recibido y el de fecha límite más próxima primero), se
mejora cada una con traslados e intercambios y se devuelve la mejor. Nunca devuelve un orden
con objetivo peor que el recibido. «Sin conexión» (``None``) jamás cuenta como costo cero.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

Matrix = Sequence[Sequence[float | None]]

MAX_PASSES = 50
MAX_STOPS = 50
# Un minuto de retraso pesa mucho más que un minuto de ruta: primero se cumple, luego se acorta.
LATENESS_SCALE = 1_000.0
DEFAULT_SERVICE_MINUTES = 0.0
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Window:
    earliest: float = 0.0
    latest: float = math.inf


@dataclass(frozen=True, slots=True)
class WindowEvaluation:
    arrivals: list[float]
    weighted_lateness: float
    late_stops: int
    finish_minutes: float

    @property
    def feasible(self) -> bool:
        return self.late_stops == 0 and math.isfinite(self.finish_minutes)

    @property
    def objective(self) -> float:
        return self.weighted_lateness * LATENESS_SCALE + self.finish_minutes


@dataclass(frozen=True, slots=True)
class WindowPlan:
    route: list[int]
    before: WindowEvaluation
    after: WindowEvaluation


def evaluate_route(
    matrix: Matrix,
    route: Sequence[int],
    windows: Mapping[int, Window],
    priority: Mapping[int, float] | None = None,
    *,
    service_minutes: float = DEFAULT_SERVICE_MINUTES,
) -> WindowEvaluation:
    """Llegadas, retraso ponderado y fin de ruta; infinito si algún tramo no tiene conexión."""
    weights = priority or {}
    clock = 0.0
    arrivals = [0.0]
    lateness = 0.0
    late = 0
    for previous, node in zip(route, route[1:], strict=False):
        leg = matrix[previous][node]
        if leg is None:
            return WindowEvaluation(arrivals, math.inf, len(route), math.inf)
        arrive = clock + leg
        window = windows.get(node)
        if window is not None:
            arrive = max(arrive, window.earliest)
            if arrive > window.latest + _EPS:
                lateness += (arrive - window.latest) * weights.get(node, 1.0)
                late += 1
        arrivals.append(arrive)
        clock = arrive + service_minutes
    return WindowEvaluation(arrivals, lateness, late, clock)


def plan_with_windows(
    matrix: Matrix,
    route: Sequence[int],
    windows: Mapping[int, Window],
    priority: Mapping[int, float] | None = None,
    *,
    service_minutes: float = DEFAULT_SERVICE_MINUTES,
) -> WindowPlan:
    """Mejor orden conocido respetando ventanas y prioridades; el origen no se mueve."""
    current = list(route)
    if len(current) > MAX_STOPS + 1:
        raise ValueError(f"Se admiten como máximo {MAX_STOPS} paradas")
    if len(set(current)) != len(current):
        raise ValueError("La ruta repite paradas")

    def score(candidate: Sequence[int]) -> float:
        return evaluate_route(
            matrix, candidate, windows, priority, service_minutes=service_minutes
        ).objective

    before = evaluate_route(matrix, current, windows, priority, service_minutes=service_minutes)
    if len(current) < 3:
        return WindowPlan(current, before, before)

    seeds = [current, _earliest_deadline_first(current, windows)]
    best = current
    best_score = before.objective
    for seed in seeds:
        candidate = _descend(seed, score)
        value = score(candidate)
        if value < best_score - _EPS:
            best, best_score = candidate, value
    after = evaluate_route(matrix, best, windows, priority, service_minutes=service_minutes)
    return WindowPlan(best, before, after)


def _earliest_deadline_first(route: Sequence[int], windows: Mapping[int, Window]) -> list[int]:
    stops = sorted(
        route[1:], key=lambda node: windows.get(node, Window()).latest
    )
    return [route[0], *stops]


def _descend(route: Sequence[int], score: Callable[[Sequence[int]], float]) -> list[int]:
    current = list(route)
    value = score(current)
    for _ in range(MAX_PASSES):
        improved = False
        size = len(current)
        for source in range(1, size):  # traslado de una parada a otra posición
            for target in range(1, size):
                if source == target:
                    continue
                candidate = list(current)
                node = candidate.pop(source)
                candidate.insert(target, node)
                candidate_value = score(candidate)
                if candidate_value < value - _EPS:
                    current, value, improved = candidate, candidate_value, True
        for first in range(1, size - 1):  # intercambio de dos paradas
            for second in range(first + 1, size):
                candidate = list(current)
                candidate[first], candidate[second] = candidate[second], candidate[first]
                candidate_value = score(candidate)
                if candidate_value < value - _EPS:
                    current, value, improved = candidate, candidate_value, True
        if not improved:
            break
    return current
