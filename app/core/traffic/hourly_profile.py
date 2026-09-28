"""Predicción de la congestión por hora local a partir de mediciones guardadas.

Cada medición de TomTom trae un multiplicador (tiempo observado / tiempo a flujo libre). Con
las mediciones de semanas anteriores se arma, por hora local de Bogotá, la mediana del
multiplicador en la ciudad y en cada celda de ~1,1 km. La predicción busca primero la celda y
la hora; si no hay suficientes muestras usa la mediana de la ciudad a esa hora, y si tampoco
alcanza devuelve 1,0 con nivel ``none``: sin datos no se inventa tráfico.

``backtest`` mide el error con datos que el perfil no vio (los últimos por tiempo), para
comparar contra suponer siempre flujo libre antes de fiarse de la predicción.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Literal

BOGOTA_OFFSET = timedelta(hours=-5)
CELL_DEGREES = 0.01
MIN_SAMPLES_ZONE = 5
MIN_SAMPLES_CITY = 8
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 4.0

Level = Literal["zone", "city", "none"]


@dataclass(frozen=True, slots=True)
class Observation:
    lat: float
    lng: float
    multiplier: float | None
    observed_at: datetime  # UTC sin zona
    road_closure: bool = False


@dataclass(frozen=True, slots=True)
class Prediction:
    multiplier: float
    level: Level
    samples: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    tested: int
    mae_profile: float
    mae_free_flow: float

    @property
    def improves(self) -> bool:
        return self.tested > 0 and self.mae_profile < self.mae_free_flow


Cell = tuple[int, int]


def local_hour(moment: datetime) -> int:
    return (moment + BOGOTA_OFFSET).hour


def _cell(lat: float, lng: float) -> Cell:
    return (math.floor(lat / CELL_DEGREES), math.floor(lng / CELL_DEGREES))


def _usable(item: Observation) -> bool:
