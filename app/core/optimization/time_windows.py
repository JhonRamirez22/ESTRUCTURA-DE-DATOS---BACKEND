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
import re
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


_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _clock_minutes(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    match = _CLOCK.match(value.strip())
    return None if match is None else int(match.group(1)) * 60 + int(match.group(2))


def window_from_json(raw: object, now_minute_of_day: float) -> Window | None:
    """Ventana de ``{"start": "HH:MM", "end": "HH:MM"}`` (hora local) a minutos desde ahora.

    Devuelve ``None`` si el valor no tiene esa forma o la hora de fin no es posterior a la de
    inicio: una ventana mal escrita se ignora, no inventa un plazo.
    """
    if not isinstance(raw, Mapping):
        return None
    start = _clock_minutes(raw.get("start"))
    end = _clock_minutes(raw.get("end"))
    if start is None and end is None:
        return None
    if start is not None and end is not None and end <= start:
        return None
    earliest = 0.0 if start is None else max(0.0, start - now_minute_of_day)
    latest = math.inf if end is None else end - now_minute_of_day
    return Window(earliest=earliest, latest=latest)


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
