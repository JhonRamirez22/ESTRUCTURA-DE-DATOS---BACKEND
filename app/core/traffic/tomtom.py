"""Tipos y normalización pura de respuestas de TomTom Traffic Flow."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal


class TrafficLevel(StrEnum):
    FLUIDO = "FLUIDO"
    MODERADO = "MODERADO"
    ALTO = "ALTO"
    MUY_ALTO = "MUY_ALTO"
    CERRADO = "CERRADO"
    DESCONOCIDO = "DESCONOCIDO"


type TrafficCacheStatus = Literal["LIVE", "CACHE", "STALE"]


@dataclass(frozen=True, slots=True)
class TrafficData:
    current_speed_kmh: float
    free_flow_speed_kmh: float
    current_travel_time_seconds: float
    free_flow_travel_time_seconds: float
    confidence: float
    road_closure: bool
    traffic_ratio: float | None
    traffic_level: TrafficLevel
    travel_time_multiplier: float | None
    timestamp: datetime
    cache_status: TrafficCacheStatus = "LIVE"


def parse_tomtom_flow_response(
    payload: object,
    *,
    timestamp: datetime | None = None,
) -> TrafficData:
    """Valida solo los campos de dominio; los tiempos de TomTom permanecen en segundos."""
    if not isinstance(payload, dict) or not isinstance(payload.get("flowSegmentData"), dict):
        raise ValueError("TomTom no devolvió flowSegmentData.")
    flow = payload["flowSegmentData"]
    current_speed = _read_nonnegative_number(flow, "currentSpeed")
    free_flow_speed = _read_nonnegative_number(flow, "freeFlowSpeed")
    current_time = _read_nonnegative_number(flow, "currentTravelTime")
    free_flow_time = _read_nonnegative_number(flow, "freeFlowTravelTime")
    confidence = _read_nonnegative_number(flow, "confidence")
    if confidence > 1:
        raise ValueError("TomTom devolvió una confianza fuera del rango 0..1.")
    road_closure = flow.get("roadClosure")
    if not isinstance(road_closure, bool):
        raise ValueError("TomTom devolvió un valor inválido para roadClosure.")

    speed_ratio = current_speed / free_flow_speed if free_flow_speed > 0 else None
    level = classify_traffic(speed_ratio, road_closure)
    time_multiplier = (
        current_time / free_flow_time
        if free_flow_time > 0 and current_time > 0
        else None
    )
    return TrafficData(
        current_speed_kmh=current_speed,
        free_flow_speed_kmh=free_flow_speed,
        current_travel_time_seconds=current_time,
        free_flow_travel_time_seconds=free_flow_time,
        confidence=confidence,
        road_closure=road_closure,
        traffic_ratio=speed_ratio,
        traffic_level=level,
        travel_time_multiplier=time_multiplier,
        timestamp=timestamp or datetime.now(UTC),
    )


def classify_traffic(speed_ratio: float | None, road_closure: bool) -> TrafficLevel:
    if road_closure:
        return TrafficLevel.CERRADO
    if speed_ratio is None or not math.isfinite(speed_ratio) or speed_ratio < 0:
        return TrafficLevel.DESCONOCIDO
    if speed_ratio >= 0.80:
        return TrafficLevel.FLUIDO
    if speed_ratio >= 0.60:
        return TrafficLevel.MODERADO
    if speed_ratio >= 0.35:
        return TrafficLevel.ALTO
    return TrafficLevel.MUY_ALTO


def _read_nonnegative_number(value: dict[str, object], field: str) -> float:
    candidate = value.get(field)
    if (
        isinstance(candidate, bool)
        or not isinstance(candidate, (int, float))
        or not math.isfinite(candidate)
        or candidate < 0
    ):
        raise ValueError(f"TomTom devolvió un valor inválido para {field}.")
    return float(candidate)
