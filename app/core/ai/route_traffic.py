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

    def as_payload(self) -> dict[str, object]:
        timestamp = self.generated_at.astimezone(UTC).isoformat(timespec="milliseconds")
        return {
            "contractVersion": ROUTE_TRAFFIC_AI_CONTRACT_VERSION,
            "task": "route_traffic",
            "city": TRAFFIC_AI_CITY,
            "generatedAt": timestamp.replace("+00:00", "Z"),
            "courierId": self.courier_id,
            "candidates": [candidate.as_payload() for candidate in self.candidates],
        }


def haversine_distance_meters(from_point: dict[str, float], to_point: dict[str, float]) -> float:
    latitude_delta = math.radians(to_point["lat"] - from_point["lat"])
    longitude_delta = math.radians(to_point["lng"] - from_point["lng"])
    from_latitude = math.radians(from_point["lat"])
    to_latitude = math.radians(to_point["lat"])
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(from_latitude) * math.cos(to_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def build_route_traffic_candidates(
    stop_ids: Sequence[str],
    coordinates: Sequence[Coordinate],
    matrix: Sequence[Sequence[float | None]],
    *,
    distance_matrix: Sequence[Sequence[float | None]] | None = None,
    distance_source: str = "road-network",
) -> list[RouteTrafficCandidate]:
    expected_size = len(stop_ids)
    if len(coordinates) != expected_size or len(matrix) != expected_size or any(
        len(row) != expected_size for row in matrix
    ):
        raise ValueError("Las paradas, coordenadas y matriz de tiempos deben tener igual tamaño.")
    if distance_matrix is not None and (
        len(distance_matrix) != expected_size
        or any(len(row) != expected_size for row in distance_matrix)
    ):
        raise ValueError("La matriz de distancias debe ser cuadrada y corresponder a las paradas.")

    candidates: list[RouteTrafficCandidate] = []
    for from_index, from_stop_id in enumerate(stop_ids):
        for to_index, to_stop_id in enumerate(stop_ids):
            if from_index == to_index:
                continue
            from_coordinate = coordinates[from_index]
            to_coordinate = coordinates[to_index]
            duration = matrix[from_index][to_index]
            if duration is None:
                continue
            from_lat_lng = {"lat": from_coordinate[1], "lng": from_coordinate[0]}
            to_lat_lng = {"lat": to_coordinate[1], "lng": to_coordinate[0]}
            raw_road_distance = (
                distance_matrix[from_index][to_index] if distance_matrix is not None else None
            )
            road_distance = (
                raw_road_distance
                if raw_road_distance is not None
                and math.isfinite(raw_road_distance)
                and raw_road_distance >= 0
                else None
            )
            distance_meters = (
                road_distance
                if road_distance is not None
                else (
                    None
                    if distance_matrix is not None
                    else haversine_distance_meters(from_lat_lng, to_lat_lng)
                )
            )
            candidates.append(
                RouteTrafficCandidate(
                    from_stop_id=from_stop_id,
                    to_stop_id=to_stop_id,
                    from_coordinates=from_lat_lng,
