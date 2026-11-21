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


def test_falls_back_to_city_then_to_free_flow() -> None:
    profile = HourlyProfile(_simulated(14))
    far = profile.predict(1.30, -77.20, START + timedelta(days=20, hours=18 + 5))
    assert far.level == "city" and abs(far.multiplier - 2.4) < 0.15
    assert HourlyProfile([]).predict(LAT, LNG, START).level == "none"
    assert HourlyProfile([]).predict(LAT, LNG, START).multiplier == 1.0


def test_too_few_samples_never_produce_a_prediction() -> None:
    few = HourlyProfile(_simulated(2))  # 2 muestras por hora: bajo el mínimo
    assert few.predict(LAT, LNG, START + timedelta(hours=18 + 5)).level == "none"


def test_closures_nulls_and_absurd_values_are_ignored() -> None:
    noisy = _simulated(14)
    when = START + timedelta(days=20, hours=18 + 5)
    baseline = HourlyProfile(noisy).predict(LAT, LNG, when).multiplier
    junk = [
        Observation(LAT, LNG, None, when),
        Observation(LAT, LNG, 9.0, when),
        Observation(LAT, LNG, 0.0, when),
        Observation(LAT, LNG, float("nan"), when),
        Observation(LAT, LNG, 3.5, when, road_closure=True),
    ] * 20
    assert HourlyProfile(noisy + junk).predict(LAT, LNG, when).multiplier == baseline


def test_predict_minutes_uses_the_hour_of_arrival() -> None:
    profile = HourlyProfile(_simulated(14))
    depart = START + timedelta(days=20, hours=16 + 5, minutes=50)  # salen a las 16:50
    minutes, prediction = profile.predict_minutes(20.0, LAT, LNG, depart)  # llegan a las 17:10
    assert prediction.multiplier > 1.8
    assert abs(minutes - 20.0 * prediction.multiplier) < 1e-9


def test_backtest_beats_assuming_free_flow_on_simulated_data() -> None:
    result = backtest(_simulated(21))
    assert result.tested > 100 and result.improves
    assert result.mae_profile < 0.5 * result.mae_free_flow


def test_backtest_without_enough_history_reports_nothing() -> None:
    result = backtest(_simulated(1))
    assert result.tested == 0 and not result.improves
