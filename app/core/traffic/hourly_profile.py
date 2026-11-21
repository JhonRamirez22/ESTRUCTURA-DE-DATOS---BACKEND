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
    return (
        item.multiplier is not None
        and not item.road_closure
        and math.isfinite(item.multiplier)
        and MIN_MULTIPLIER <= item.multiplier <= MAX_MULTIPLIER
        and math.isfinite(item.lat)
        and math.isfinite(item.lng)
    )


class HourlyProfile:
    def __init__(self, observations: Sequence[Observation]) -> None:
        by_zone: dict[tuple[Cell, int], list[float]] = defaultdict(list)
        by_city: dict[int, list[float]] = defaultdict(list)
        for item in observations:
            if not _usable(item):
                continue
            multiplier = float(item.multiplier or 1.0)
            hour = local_hour(item.observed_at)
            by_zone[(_cell(item.lat, item.lng), hour)].append(multiplier)
            by_city[hour].append(multiplier)
        self._zone = {
            key: (median(values), len(values))
            for key, values in by_zone.items()
            if len(values) >= MIN_SAMPLES_ZONE
        }
        self._city = {
            hour: (median(values), len(values))
            for hour, values in by_city.items()
            if len(values) >= MIN_SAMPLES_CITY
        }

    def predict(self, lat: float, lng: float, when: datetime) -> Prediction:
        hour = local_hour(when)
        zone = self._zone.get((_cell(lat, lng), hour))
        if zone is not None:
            return Prediction(zone[0], "zone", zone[1])
        city = self._city.get(hour)
        if city is not None:
            return Prediction(city[0], "city", city[1])
        return Prediction(1.0, "none", 0)

    def predict_minutes(
        self, base_minutes: float, lat: float, lng: float, depart_at: datetime
    ) -> tuple[float, Prediction]:
        """Tiempo con la congestión esperada a la hora en que se llegaría al tramo."""
        arrival = depart_at + timedelta(minutes=base_minutes)
        prediction = self.predict(lat, lng, arrival)
        return base_minutes * prediction.multiplier, prediction


def backtest(observations: Sequence[Observation], *, holdout: float = 0.3) -> BacktestResult:
    """Entrena con lo más antiguo y mide el error absoluto del multiplicador en lo más nuevo."""
    usable = sorted((item for item in observations if _usable(item)), key=lambda i: i.observed_at)
    split = int(len(usable) * (1 - holdout))
    train, test = usable[:split], usable[split:]
    profile = HourlyProfile(train)
    errors_profile: list[float] = []
    errors_free: list[float] = []
    for item in test:
        prediction = profile.predict(item.lat, item.lng, item.observed_at)
        if prediction.level == "none":
            continue  # sin datos para esa hora: no cuenta ni a favor ni en contra
        actual = float(item.multiplier or 1.0)
        errors_profile.append(abs(prediction.multiplier - actual))
        errors_free.append(abs(1.0 - actual))
    if not errors_profile:
        return BacktestResult(0, 0.0, 0.0)
    n = len(errors_profile)
    return BacktestResult(n, sum(errors_profile) / n, sum(errors_free) / n)
