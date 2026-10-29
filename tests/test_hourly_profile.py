"""Perfil horario de congestión con datos SIMULADOS (nunca mediciones reales)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.core.traffic.hourly_profile import (
    HourlyProfile,
    Observation,
    backtest,
    local_hour,
)

# Picos simulados en hora local de Bogotá (UTC-5), como en seed_simulated_day.
PEAKS = {7: 1.9, 8: 1.7, 17: 2.1, 18: 2.4, 19: 1.8}
LAT, LNG = 1.2154, -77.2777
START = datetime(2026, 10, 1, 0, 0)  # UTC


def _simulated(days: int, *, seed: int = 1, noise: float = 0.08) -> list[Observation]:
    rng = random.Random(seed)
    rows: list[Observation] = []
    for day in range(days):
        for local in range(24):
            utc = START + timedelta(days=day, hours=local + 5, minutes=10)
            truth = PEAKS.get(local, 1.05)
            rows.append(Observation(LAT, LNG, truth * (1 + rng.uniform(-noise, noise)), utc))
    return rows


def test_local_hour_uses_bogota_time() -> None:
    assert local_hour(datetime(2026, 11, 20, 22, 30)) == 17
    assert local_hour(datetime(2026, 11, 20, 3, 0)) == 22  # día anterior en Bogotá


def test_profile_recovers_simulated_peaks_within_noise() -> None:
    profile = HourlyProfile(_simulated(14))
    for hour, truth in PEAKS.items():
        when = START + timedelta(days=20, hours=hour + 5)
        prediction = profile.predict(LAT, LNG, when)
        assert prediction.level == "zone"
        assert abs(prediction.multiplier - truth) < 0.12
    night = profile.predict(LAT, LNG, START + timedelta(days=20, hours=3 + 5))
    assert abs(night.multiplier - 1.05) < 0.1

