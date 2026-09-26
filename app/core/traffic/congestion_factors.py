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
