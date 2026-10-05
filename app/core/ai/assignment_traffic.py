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
    candidates: tuple[AssignmentCandidate, ...]
    generated_at: datetime

    def as_payload(self) -> dict[str, object]:
        generated_at = self.generated_at.astimezone(UTC).isoformat(timespec="milliseconds")
        return {
            "contractVersion": TRAFFIC_AI_CONTRACT_VERSION,
            "task": "assignment_traffic",
            "city": TRAFFIC_AI_CITY,
            "generatedAt": generated_at.replace("+00:00", "Z"),
            "candidates": [candidate.as_payload() for candidate in self.candidates],
        }


def build_assignment_candidates(
    couriers: Sequence[tuple[str, Coordinate]],
    orders: Sequence[tuple[str, Coordinate]],
    *,
    road_metrics: Mapping[
        tuple[str, str], tuple[float | None, float | None]
    ] | None = None,
) -> list[AssignmentCandidate]:
    candidates: list[AssignmentCandidate] = []
    for courier_id, courier_coordinate in couriers:
        courier_lat_lng = {"lat": courier_coordinate[1], "lng": courier_coordinate[0]}
        for order_id, order_coordinate in orders:
            order_lat_lng = {"lat": order_coordinate[1], "lng": order_coordinate[0]}
            metrics = (
                road_metrics.get((courier_id, order_id))
                if road_metrics is not None
                else None
            )
            if road_metrics is not None and (metrics is None or metrics[1] is None):
                continue
            road_distance = metrics[0] if metrics is not None else None
            candidates.append(
                AssignmentCandidate(
                    courier_id=courier_id,
                    order_id=order_id,
                    courier_coordinates=courier_lat_lng,
                    delivery_coordinates=order_lat_lng,
                    distance_meters=(
                        road_distance
                        if road_distance is not None
                        else haversine_distance_meters(courier_lat_lng, order_lat_lng)
                    ),
                    distance_source=(
                        "road" if road_distance is not None else "straight-line-estimate"
                    ),
                    baseline_duration_minutes=(metrics[1] if metrics is not None else None),
                )
            )
    return candidates


def parse_assignment_traffic_response(payload: object) -> AssignmentTrafficAdvice:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("adjustments"), list):
        raise ValueError("La respuesta de IA no cumple el contrato de ajustes de asignación.")
    adjustments: list[AssignmentTrafficAdjustment] = []
