"""Contrato privado y validación de factores IA para propuestas de asignación."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.ai.route_traffic import TRAFFIC_AI_CITY, haversine_distance_meters
from app.core.routing.contracts import Coordinate

TRAFFIC_AI_CONTRACT_VERSION = "rutas-pasto.traffic-assignment.v1"
MIN_TRAFFIC_MULTIPLIER = 0.5
MAX_TRAFFIC_MULTIPLIER = 3.0
MAX_ASSIGNMENT_TRAFFIC_AI_CANDIDATES = 64


@dataclass(frozen=True, slots=True)
class AssignmentCandidate:
    courier_id: str
    order_id: str
    courier_coordinates: dict[str, float]
    delivery_coordinates: dict[str, float]
    distance_meters: float
    distance_source: str = "straight-line-estimate"
    baseline_duration_minutes: float | None = None
    historical_congestion_factor: float = 1.0

    def as_payload(self) -> dict[str, object]:
        return {
            "courierId": self.courier_id,
            "orderId": self.order_id,
            "courierCoordinates": self.courier_coordinates,
            "deliveryCoordinates": self.delivery_coordinates,
            "distanceMeters": self.distance_meters,
            "distanceSource": self.distance_source,
            "baselineDurationMinutes": self.baseline_duration_minutes,
            "historicalCongestionFactor": self.historical_congestion_factor,
        }


@dataclass(frozen=True, slots=True)
class AssignmentTrafficAdjustment:
    courier_id: str
    order_id: str
    traffic_multiplier: float
    confidence: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentTrafficAdvice:
    adjustments: tuple[AssignmentTrafficAdjustment, ...]
    model: str | None = None


@dataclass(frozen=True, slots=True)
class AssignmentTrafficRequest:
