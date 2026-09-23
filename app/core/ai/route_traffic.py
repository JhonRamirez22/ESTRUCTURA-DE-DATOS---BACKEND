"""Contratos puros y validación estricta para factores de tráfico por tramo."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.routing.contracts import Coordinate

ROUTE_TRAFFIC_AI_CONTRACT_VERSION = "rutas-pasto.route-traffic.v1"
TRAFFIC_AI_CITY = "Pasto, Nariño, Colombia"
MIN_TRAFFIC_MULTIPLIER = 0.5
MAX_TRAFFIC_MULTIPLIER = 3.0
MAX_ROUTE_TRAFFIC_AI_CANDIDATES = 32
EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True, slots=True)
class RouteTrafficCandidate:
    from_stop_id: str
    to_stop_id: str
    from_coordinates: dict[str, float]
    to_coordinates: dict[str, float]
    distance_meters: float | None
    distance_source: str
    baseline_duration_minutes: float
    historical_congestion_factor: float = 1.0

    def as_payload(self) -> dict[str, object]:
        return {
            "fromStopId": self.from_stop_id,
            "toStopId": self.to_stop_id,
            "fromCoordinates": self.from_coordinates,
            "toCoordinates": self.to_coordinates,
            "distanceMeters": self.distance_meters,
            "distanceSource": self.distance_source,
            "baselineDurationMinutes": self.baseline_duration_minutes,
            "historicalCongestionFactor": self.historical_congestion_factor,
        }


@dataclass(frozen=True, slots=True)
class RouteTrafficAdjustment:
    from_stop_id: str
    to_stop_id: str
    traffic_multiplier: float
    confidence: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RouteTrafficAdvice:
    adjustments: tuple[RouteTrafficAdjustment, ...]
    model: str | None = None


@dataclass(frozen=True, slots=True)
class RouteTrafficAiRequest:
    candidates: tuple[RouteTrafficCandidate, ...]
    generated_at: datetime
    courier_id: str = "courier"
    traffic_observations: tuple[dict[str, object], ...] = ()

    def as_payload(self) -> dict[str, object]:
        timestamp = self.generated_at.astimezone(UTC).isoformat(timespec="milliseconds")
        return {
            "contractVersion": ROUTE_TRAFFIC_AI_CONTRACT_VERSION,
            "task": "route_traffic",
            "city": TRAFFIC_AI_CITY,
            "generatedAt": timestamp.replace("+00:00", "Z"),
