"""Factores robustos de tiempos observados por tramo y hora local."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

DEFAULT_MIN_SAMPLES = 3
DEFAULT_MIN_FACTOR = 0.5
DEFAULT_MAX_FACTOR = 3.0


@dataclass(frozen=True, slots=True)
class TravelTimeSample:
    segment_id: str
    hour_of_day: int
    ors_duration_minutes: float
    observed_duration_minutes: float


def calculate_congestion_factors(
    samples: Sequence[TravelTimeSample],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_factor: float = DEFAULT_MIN_FACTOR,
    max_factor: float = DEFAULT_MAX_FACTOR,
) -> dict[str, float]:
    _validate_options(min_samples, min_factor, max_factor)
    ratios: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        if _valid_sample(sample):
            ratios[build_congestion_factor_key(sample.segment_id, sample.hour_of_day)].append(
                sample.observed_duration_minutes / sample.ors_duration_minutes
            )
    return {
        key: (
            min(max(median(values), min_factor), max_factor) if len(values) >= min_samples else 1.0
        )
        for key, values in ratios.items()
    }


def get_congestion_factor(
    factors: dict[str, float],
    segment_id: str,
    hour_of_day: int,
) -> float:
    return factors.get(build_congestion_factor_key(segment_id, hour_of_day), 1.0)


def apply_congestion_factor(ors_duration_minutes: float, factor: float) -> float:
    if (
        not math.isfinite(ors_duration_minutes)
        or ors_duration_minutes < 0
        or not math.isfinite(factor)
        or factor < 0
    ):
        raise ValueError("duración y factor deben ser finitos y no negativos.")
    return ors_duration_minutes * factor


def build_congestion_factor_key(segment_id: str, hour_of_day: int) -> str:
    if not segment_id.strip():
        raise ValueError("segment_id no puede estar vacío.")
    _validate_hour(hour_of_day)
    return f"{segment_id}:{hour_of_day}"


def _valid_sample(sample: TravelTimeSample) -> bool:
    return (
        bool(sample.segment_id.strip())
        and isinstance(sample.hour_of_day, int)
        and 0 <= sample.hour_of_day <= 23
        and math.isfinite(sample.ors_duration_minutes)
        and sample.ors_duration_minutes > 0
        and math.isfinite(sample.observed_duration_minutes)
        and sample.observed_duration_minutes > 0
    )


def _validate_options(min_samples: int, min_factor: float, max_factor: float) -> None:
    if min_samples < 1:
        raise ValueError("min_samples debe ser un entero positivo.")
    if (
        not math.isfinite(min_factor)
        or not math.isfinite(max_factor)
        or min_factor < 0
        or min_factor > max_factor
    ):
        raise ValueError("Los límites del factor de congestión no son válidos.")


def _validate_hour(hour_of_day: int) -> None:
    if not isinstance(hour_of_day, int) or not 0 <= hour_of_day <= 23:
        raise ValueError("hour_of_day debe ser un entero entre 0 y 23.")
