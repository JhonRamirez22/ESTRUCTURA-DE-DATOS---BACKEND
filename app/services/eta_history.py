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
        "observations": len(rows),
        "simulatedShare": round(simulated / len(rows), 2) if rows else 0.0,
        "backtest": {
            "tested": result.tested,
            "maeProfile": round(result.mae_profile, 3),
            "maeFreeFlow": round(result.mae_free_flow, 3),
            "improves": result.improves,
        },
    }
    return HourlyProfile(observations), meta


async def historical_eta(
    repository: Any, stops: Sequence[StopForEta], now: datetime
) -> dict[str, Any] | None:
    """Predicción por parada, o ``None`` si no hay historial utilizable."""
    global _CACHE  # noqa: PLW0603 - caché del perfil por proceso, con vencimiento
    clock = time.monotonic()
    if _CACHE is None or clock - _CACHE.built_at > PROFILE_TTL_SECONDS:
        rows = await repository.list_recent_traffic_observations(
            now - timedelta(days=HISTORY_DAYS)
        )
        if not rows:
            return None
        profile, meta = _build(rows)
        _CACHE = _Cached(clock, profile, meta)
    profile, meta = _CACHE.profile, _CACHE.meta
    per_stop: dict[str, dict[str, Any]] = {}
    for stop in stops:
        if stop.base_minutes is None:
            continue
        minutes, prediction = profile.predict_minutes(stop.base_minutes, stop.lat, stop.lng, now)
        if prediction.level == "none":
            continue
        per_stop[stop.stop_id] = {
            "minutes": round(minutes, 1),
            "multiplier": round(prediction.multiplier, 2),
            "level": prediction.level,
            "samples": prediction.samples,
        }
    if not per_stop:
        return None
    return {"summary": meta, "perStop": per_stop}


def reset_cache() -> None:
    global _CACHE  # noqa: PLW0603
    _CACHE = None
