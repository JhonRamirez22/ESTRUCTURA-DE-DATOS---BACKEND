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


