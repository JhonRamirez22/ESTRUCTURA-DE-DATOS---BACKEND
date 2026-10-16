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
            "courierId": self.courier_id,
            "candidates": [candidate.as_payload() for candidate in self.candidates],
            "trafficObservations": list(self.traffic_observations),
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
                    to_coordinates=to_lat_lng,
                    distance_meters=distance_meters,
                    distance_source=(
                        distance_source
                        if road_distance is not None
                        else "unavailable"
                        if distance_matrix is not None
                        else "straight-line-estimate"
                    ),
                    baseline_duration_minutes=duration,
                )
            )
    return candidates


def select_route_traffic_candidates(
    candidates: Sequence[RouteTrafficCandidate],
    baseline_path_stop_ids: Sequence[str],
    max_candidates: int = MAX_ROUTE_TRAFFIC_AI_CANDIDATES,
) -> list[RouteTrafficCandidate]:
    if max_candidates < 1:
        raise ValueError("max_candidates debe ser positivo")
    by_pair: dict[tuple[str, str], RouteTrafficCandidate] = {}
    for candidate in candidates:
        duration = candidate.baseline_duration_minutes
        pair = (candidate.from_stop_id, candidate.to_stop_id)
        if (
            candidate.from_stop_id != candidate.to_stop_id
            and math.isfinite(duration)
            and duration >= 0
        ):
            by_pair.setdefault(pair, candidate)

    preferred_pairs = set(zip(baseline_path_stop_ids, baseline_path_stop_ids[1:], strict=False))
    compare = lambda item: (  # noqa: E731
        item.baseline_duration_minutes,
        item.from_stop_id,
        item.to_stop_id,
    )
    preferred = sorted(
        (candidate for pair, candidate in by_pair.items() if pair in preferred_pairs),
        key=compare,
        reverse=True,
    )
    selected = preferred[: math.ceil(max_candidates / 2)]
    selected_pairs = {(item.from_stop_id, item.to_stop_id) for item in selected}
    alternatives = sorted(
        (candidate for pair, candidate in by_pair.items() if pair not in selected_pairs),
        key=compare,
    )
    selected.extend(alternatives[: max_candidates - len(selected)])
    return selected


def parse_route_traffic_response(payload: object) -> RouteTrafficAdvice:
    if not isinstance(payload, dict) or not isinstance(payload.get("adjustments"), list):
        raise ValueError("La respuesta de IA no cumple el contrato de ajustes de tráfico.")

    adjustments: list[RouteTrafficAdjustment] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, value in enumerate(payload["adjustments"]):
        if not isinstance(value, dict):
            raise ValueError(f"El ajuste de IA en la posición {index} no es un objeto.")
        from_id = _read_nonempty_string(value, "fromStopId", index)
        to_id = _read_nonempty_string(value, "toStopId", index)
        pair = (from_id, to_id)
        if pair in seen_pairs:
            raise ValueError(f"La respuesta de IA repite el par {from_id}/{to_id}.")
        seen_pairs.add(pair)
        multiplier = _read_finite_number(value, "trafficMultiplier", index)
        if not MIN_TRAFFIC_MULTIPLIER <= multiplier <= MAX_TRAFFIC_MULTIPLIER:
            raise ValueError(
                f"El factor de IA para {from_id}/{to_id} debe estar entre "
                f"{MIN_TRAFFIC_MULTIPLIER} y {MAX_TRAFFIC_MULTIPLIER}."
            )
        confidence_value = value.get("confidence")
        confidence: float | None = None
        if confidence_value is not None:
            confidence = _read_finite_number(value, "confidence", index)
            if not 0 <= confidence <= 1:
                raise ValueError(
                    f"La confianza de IA en la posición {index} debe estar entre 0 y 1."
                )
        reason_value = value.get("reason")
        adjustments.append(
            RouteTrafficAdjustment(
                from_stop_id=from_id,
                to_stop_id=to_id,
                traffic_multiplier=multiplier,
                confidence=confidence,
                reason=reason_value if isinstance(reason_value, str) else None,
            )
        )
    model = payload.get("model")
    return RouteTrafficAdvice(tuple(adjustments), model if isinstance(model, str) else None)


def filter_route_traffic_adjustments(
    candidates: Sequence[RouteTrafficCandidate],
    adjustments: Sequence[RouteTrafficAdjustment],
) -> tuple[list[RouteTrafficAdjustment], int]:
    allowed = {(candidate.from_stop_id, candidate.to_stop_id) for candidate in candidates}
    supported = [
        adjustment
        for adjustment in adjustments
        if (adjustment.from_stop_id, adjustment.to_stop_id) in allowed
    ]
    return supported, len(adjustments) - len(supported)


def apply_route_traffic_adjustments(
    matrix: Sequence[Sequence[float | None]],
    stop_ids: Sequence[str],
    adjustments: Sequence[RouteTrafficAdjustment],
) -> list[list[float | None]]:
    index_by_stop = {stop_id: index for index, stop_id in enumerate(stop_ids)}
    adjusted = [list(row) for row in matrix]
    for adjustment in adjustments:
        from_index = index_by_stop.get(adjustment.from_stop_id)
        to_index = index_by_stop.get(adjustment.to_stop_id)
        if from_index is None or to_index is None:
            continue
        current_duration = adjusted[from_index][to_index]
        if current_duration is not None:
            adjusted[from_index][to_index] = current_duration * adjustment.traffic_multiplier
    return adjusted


def _read_nonempty_string(value: dict[str, object], field: str, index: int) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"El campo {field} del ajuste de IA {index} debe ser un string.")
    return candidate


def _read_finite_number(value: dict[str, object], field: str, index: int) -> float:
    candidate = value.get(field)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ValueError(f"El campo {field} del ajuste de IA {index} debe ser numérico.")
    result = float(candidate)
    if not math.isfinite(result):
        raise ValueError(f"El campo {field} del ajuste de IA {index} debe ser finito.")
    return result
