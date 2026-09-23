"""Estimaciones acumuladas por tramo en la secuencia actual de una ruta."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

type CostMatrix = Sequence[Sequence[float | None]]


@dataclass(frozen=True, slots=True)
class StopEta:
    stop_id: str
    distance_from_courier_meters: float | None
    estimated_minutes_from_now: float | None


def estimate_stop_etas_from_matrix(
    stops: Sequence[str],
    durations_minutes: CostMatrix,
    distances_meters: CostMatrix | None = None,
) -> list[StopEta]:
    """Suma las piernas dirigidas; una desconexión deja el resto sin ETA inventado."""
    if not stops:
        return []
    if any(not stop_id.strip() for stop_id in stops):
        raise ValueError("Los identificadores de paradas no pueden estar vacíos.")

    matrix_size = len(stops) + 1
    _validate_matrix(durations_minutes, matrix_size, "durationsMinutes")
