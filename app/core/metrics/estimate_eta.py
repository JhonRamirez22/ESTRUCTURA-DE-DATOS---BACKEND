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
    if distances_meters is not None:
        _validate_matrix(distances_meters, matrix_size, "distancesMeters")

    cumulative_minutes: float | None = 0
    cumulative_meters: float | None = 0 if distances_meters is not None else None
    etas: list[StopEta] = []
    for index, stop_id in enumerate(stops):
        duration = durations_minutes[index][index + 1]
        cumulative_minutes = _add_reachable_leg(cumulative_minutes, duration)
        if distances_meters is not None and cumulative_meters is not None:
            distance = distances_meters[index][index + 1]
            cumulative_meters = _add_reachable_leg(cumulative_meters, distance)
        etas.append(StopEta(stop_id, cumulative_meters, cumulative_minutes))
    return etas


def _add_reachable_leg(
    cumulative: float | None,
    leg: float | None,
) -> float | None:
    return None if cumulative is None or leg is None else cumulative + leg


def _validate_matrix(matrix: CostMatrix, expected_size: int, name: str) -> None:
    if len(matrix) != expected_size or any(len(row) != expected_size for row in matrix):
        raise ValueError(f"{name} debe ser cuadrada y cubrir courier más paradas.")
    for row in matrix:
        for value in row:
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} solo admite números finitos no negativos o null.")
