"""Cliente asíncrono para OSRM Table y Route, con duración en minutos."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Literal, TypedDict

import httpx

from app.core.routing.contracts import Coordinate, GeoJsonLineString, validate_coordinates
from app.core.routing.routing_errors import RecoverableRoutingError, RoutingClientError

OSRM_MAX_SNAP_DISTANCE_METERS = 50
_WAYPOINT_GEOMETRY_TOLERANCE_METERS = 25


class OsrmMatrixResult(TypedDict):
    durations_minutes: list[list[float | None]]
    distances_meters: list[list[float | None]]


class OsrmRouteResult(TypedDict):
    geometry: GeoJsonLineString
    geometry_provider: Literal["OSRM"]
    distance_meters: float
    duration_minutes: float
    snap_distances_meters: list[float]


class OsrmClientError(RoutingClientError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message, status, response_body)


class OsrmRateLimitError(OsrmClientError, RecoverableRoutingError):
    def __init__(self, response_body: str) -> None:
        super().__init__(
            f"OSRM rate limit exceeded (HTTP 429): {response_body}",
            status=429,
            response_body=response_body,
        )


class OsrmTimeoutError(OsrmClientError, RecoverableRoutingError):
    def __init__(self, timeout_seconds: float) -> None:
        timeout_ms = round(timeout_seconds * 1_000)
        super().__init__(f"OSRM request timed out after {timeout_ms} ms")
        self.timeout_seconds = timeout_seconds


class OsrmClient:
    """Reusable HTTP client; inject a transport or client for isolated tests."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser positivo y finito")
        if transport is not None and client is not None:
            raise ValueError("No se puede inyectar transport y client al mismo tiempo")

        configured_url = base_url or os.getenv("OSRM_API_URL") or "https://router.project-osrm.org"
        self._base_url = configured_url.strip().rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError("OSRM_API_URL debe comenzar con http:// o https://")
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> OsrmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> OsrmMatrixResult:
        """Consulta Table; duraciones en minutos, distancias en metros y null sin conexión."""
        validate_coordinates(coordinates)
        encoded = _serialize_coordinates(coordinates)
        radiuses = _serialize_radiuses(len(coordinates))
        payload = await self._request(
            f"/table/v1/driving/{encoded}?annotations=duration,distance&radiuses={radiuses}"
        )
        if payload.get("code") != "Ok":
            detail = payload.get("message", payload.get("code", "respuesta desconocida"))
            raise OsrmClientError(f"OSRM table response was not successful: {detail}")

        duration_seconds = _read_matrix(payload.get("durations"), "durations")
        distances_meters = _read_matrix(payload.get("distances"), "distances")
        _validate_matrix_dimensions(duration_seconds, len(coordinates), "durations")
        _validate_matrix_dimensions(distances_meters, len(coordinates), "distances")
        return {
            "durations_minutes": [
                [None if seconds is None else seconds / 60 for seconds in row]
                for row in duration_seconds
            ],
            "distances_meters": distances_meters,
        }

    async def get_route(self, coordinates: Sequence[Coordinate]) -> OsrmRouteResult:
        """Consulta Route; entrega geometría [lng,lat], metros y duración en minutos."""
        validate_coordinates(coordinates)
        encoded = _serialize_coordinates(coordinates)
        radiuses = _serialize_radiuses(len(coordinates))
        payload = await self._request(
            f"/route/v1/driving/{encoded}"
            f"?overview=full&geometries=geojson&steps=false&radiuses={radiuses}"
        )
        routes = payload.get("routes")
        if payload.get("code") != "Ok" or not isinstance(routes, list) or not routes:
            detail = payload.get("message", payload.get("code", "respuesta desconocida"))
            raise OsrmClientError(f"OSRM route response was not successful: {detail}")

        route = _read_object(routes[0], "OSRM route response contains an invalid route")
        distance = _read_number(route.get("distance"), "route distance")
        duration_seconds = _read_number(route.get("duration"), "route duration")
        geometry = _read_geometry(route.get("geometry"))
        waypoints = _read_waypoints(payload.get("waypoints"), len(coordinates))
        _validate_geometry_covers_waypoints(geometry, waypoints)

        return {
            "geometry": geometry,
            "geometry_provider": "OSRM",
            "distance_meters": distance,
            "duration_minutes": duration_seconds / 60,
            "snap_distances_meters": [distance_meters for _, distance_meters in waypoints],
        }

    async def _request(self, path: str) -> dict[str, object]:
        try:
            response = await self._client.get(path)
        except httpx.TimeoutException as error:
            raise OsrmTimeoutError(self._timeout_seconds) from error
        except httpx.RequestError as error:
            raise OsrmClientError(f"OSRM network request failed: {error}") from error

        if not response.is_success:
            body = response.text
            if response.status_code == 429:
                raise OsrmRateLimitError(body)
            raise OsrmClientError(
                f"OSRM request failed (HTTP {response.status_code}): {body}",
                status=response.status_code,
                response_body=body,
            )

        try:
            raw_payload: object = response.json()
        except ValueError as error:
            raise OsrmClientError(f"OSRM returned invalid JSON: {error}") from error
        return _read_object(raw_payload, "OSRM response must be a JSON object")


def _serialize_coordinates(coordinates: Sequence[Coordinate]) -> str:
    return ";".join(f"{longitude},{latitude}" for longitude, latitude in coordinates)


def _serialize_radiuses(count: int) -> str:
    return ";".join(str(OSRM_MAX_SNAP_DISTANCE_METERS) for _ in range(count))


def _read_object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OsrmClientError(message)
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _read_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise OsrmClientError(f"OSRM response contains an invalid {name}")
    return float(value)


def _read_matrix(value: object, name: str) -> list[list[float | None]]:
    if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
        raise OsrmClientError(f"OSRM response has no valid {name} matrix")

    result: list[list[float | None]] = []
    for row in value:
        values: list[float | None] = []
        for cell in row:
            if cell is None:
                values.append(None)
            elif (
                isinstance(cell, bool)
                or not isinstance(cell, (int, float))
                or not math.isfinite(cell)
                or cell < 0
            ):
                raise OsrmClientError(f"OSRM response contains an invalid {name} value")
            else:
                values.append(float(cell))
        result.append(values)
    return result


def _validate_matrix_dimensions(matrix: Sequence[Sequence[object]], size: int, name: str) -> None:
    if len(matrix) != size or any(len(row) != size for row in matrix):
        raise OsrmClientError(f"OSRM response has an incomplete {name} matrix")


def _read_coordinate(value: object, error_message: str) -> Coordinate:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(
            isinstance(part, bool) or not isinstance(part, (int, float)) or not math.isfinite(part)
            for part in value
        )
    ):
        raise OsrmClientError(error_message)
    longitude, latitude = float(value[0]), float(value[1])
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise OsrmClientError(error_message)
    return longitude, latitude


def _read_geometry(value: object) -> GeoJsonLineString:
    candidate = _read_object(value, "OSRM route response has no geometry")
    raw_coordinates = candidate.get("coordinates")
    if candidate.get("type") != "LineString" or not isinstance(raw_coordinates, list):
        raise OsrmClientError("OSRM route response has invalid geometry")
    if len(raw_coordinates) < 2:
        raise OsrmClientError("OSRM route response has invalid geometry")
    coordinates = [
        _read_coordinate(item, "OSRM route response has invalid geometry")
        for item in raw_coordinates
    ]
    return {"type": "LineString", "coordinates": coordinates}


def _read_waypoints(value: object, expected_count: int) -> list[tuple[Coordinate, float]]:
    if not isinstance(value, list) or len(value) != expected_count:
        raise OsrmClientError("OSRM route response has incomplete snapped waypoints")

    waypoints: list[tuple[Coordinate, float]] = []
    for index, waypoint in enumerate(value):
        try:
            candidate = _read_object(
                waypoint,
                f"OSRM returned an invalid waypoint at index {index}",
            )
            location = _read_coordinate(candidate.get("location"), "invalid snapped location")
            distance = _read_number(candidate.get("distance"), "waypoint distance")
        except OsrmClientError as error:
            raise OsrmClientError(f"OSRM returned an invalid waypoint at index {index}") from error

        if not 0 <= distance <= OSRM_MAX_SNAP_DISTANCE_METERS:
            raise OsrmClientError(
                f"OSRM could not snap waypoint {index + 1} to a nearby street within "
                f"{OSRM_MAX_SNAP_DISTANCE_METERS} m"
            )
        waypoints.append((location, distance))
    return waypoints


def _validate_geometry_covers_waypoints(
    geometry: GeoJsonLineString,
    waypoints: Sequence[tuple[Coordinate, float]],
) -> None:
    first_segment_to_check = 0
    route_coordinates = geometry["coordinates"]
    for waypoint_index, (location, _) in enumerate(waypoints):
        closest_distance = math.inf
        closest_segment_index = -1
        for segment_index in range(first_segment_to_check, len(route_coordinates) - 1):
            distance = _distance_to_segment_meters(
                location,
                route_coordinates[segment_index],
                route_coordinates[segment_index + 1],
            )
            if distance < closest_distance:
                closest_distance = distance
                closest_segment_index = segment_index

        if closest_segment_index < 0 or closest_distance > _WAYPOINT_GEOMETRY_TOLERANCE_METERS:
            raise OsrmClientError(
                f"OSRM route geometry does not pass through waypoint {waypoint_index + 1}"
            )
        # La búsqueda monotónica impide que una línea que omite una parada se acepte como ruta.
        first_segment_to_check = closest_segment_index


def _distance_to_segment_meters(
    point: Coordinate,
    start: Coordinate,
    end: Coordinate,
) -> float:
    reference_latitude = math.radians((point[1] + start[1] + end[1]) / 3)
    meters_per_longitude_degree = 111_320 * math.cos(reference_latitude)
    point_x, point_y = point[0] * meters_per_longitude_degree, point[1] * 110_540
    start_x, start_y = start[0] * meters_per_longitude_degree, start[1] * 110_540
    end_x, end_y = end[0] * meters_per_longitude_degree, end[1] * 110_540
    segment_x, segment_y = end_x - start_x, end_y - start_y
    length_squared = segment_x**2 + segment_y**2
    projection = (
        0.0
        if length_squared == 0
        else max(
            0.0,
            min(
                1.0,
                ((point_x - start_x) * segment_x + (point_y - start_y) * segment_y)
                / length_squared,
            ),
        )
    )
    closest_x = start_x + projection * segment_x
    closest_y = start_y + projection * segment_y
    return math.hypot(point_x - closest_x, point_y - closest_y)
