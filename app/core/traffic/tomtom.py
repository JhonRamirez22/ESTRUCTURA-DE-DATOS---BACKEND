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
