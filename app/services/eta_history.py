"""ETA con la congestión histórica esperada a la hora de llegada (apagado por defecto).

Solo actúa con ``TRAFFIC_HISTORY_ENABLED`` encendido y mediciones suficientes. Nunca sustituye el
ETA vial: lo acompaña con ``historical*`` para que el despachador compare. Las mediciones que
vienen de la siembra simulada (``SIM-``) se declaran como tales en la respuesta.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.traffic.hourly_profile import HourlyProfile, Observation, backtest
from app.models import TrafficObservation

HISTORY_DAYS = 60
PROFILE_TTL_SECONDS = 600.0
SIMULATED_PREFIX = "SIM-"


@dataclass(frozen=True, slots=True)
class StopForEta:
    stop_id: str
    lat: float
    lng: float
    base_minutes: float | None


@dataclass(frozen=True, slots=True)
class _Cached:
    built_at: float
    profile: HourlyProfile
    meta: dict[str, Any]


_CACHE: _Cached | None = None


def _build(rows: Sequence[TrafficObservation]) -> tuple[HourlyProfile, dict[str, Any]]:
    observations = [
        Observation(r.point_lat, r.point_lng, r.multiplier, r.observed_at, r.road_closure)
        for r in rows
    ]
    result = backtest(observations)
    simulated = sum(r.from_stop.startswith(SIMULATED_PREFIX) for r in rows)
    meta = {
