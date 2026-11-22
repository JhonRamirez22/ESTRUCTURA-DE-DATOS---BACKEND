"""Live fleet re-optimisation service: reads active routes, scores them and proposes changes.

The proposal is recomputed at most every :data:`TICK_SECONDS` for an unchanged fleet state and
is never applied automatically; the dispatcher accepts or ignores it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.routing.contracts import Coordinate, GeoJsonLineString
from app.core.routing.routing_errors import RoutingClientError
from app.core.structures.ttl_cache import TtlCache
from app.models import DeliveryPointStatus, RouteStatus
from app.repositories.route_repository import RouteRepository
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import CourierState, Fleet, GeoPoint, Route, Stop
from app.routing.reoptimizer import FleetProposal, LiveFleetReoptimizer, RouteProposal
from app.services.live_explanations import (
    ExplanationPhraser,
    LiveExplainer,
    NextStopFinder,
)

TICK_SECONDS = 10.0
MAX_GEOMETRY_ROUTES = 8
MIN_GEOMETRY_GAIN_MINUTES = 0.2
_LOGGER = logging.getLogger(__name__)
_ACTIVE = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)
_OPEN_STOP = (DeliveryPointStatus.PENDING, DeliveryPointStatus.EN_ROUTE)


class MatrixProvider(Protocol):
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> Any: ...


class GeometryProvider(Protocol):
    async def get_route(self, coordinates: Sequence[Coordinate]) -> Any: ...


@dataclass(frozen=True, slots=True)
class _FleetSnapshot:
    fleet: Fleet
    skipped: list[dict[str, str]]


class FleetSnapshotBuilder:
    """Turns persisted routes into the in-memory :class:`Fleet` the re-optimiser works on."""

    def build(self, routes: Sequence[Any]) -> _FleetSnapshot:
        built: list[Route] = []
        skipped: list[dict[str, str]] = []
        for record in routes:
            if record.status not in _ACTIVE:
                continue
            courier = record.courier
            if courier.current_lat is None or courier.current_lng is None or not (
                is_within_pasto_service_area(courier.current_lat, courier.current_lng)
            ):
                skipped.append({"routeId": str(record.id), "reason": "no-courier-position"})
                continue
            open_points = sorted(
                (p for p in record.delivery_points if p.status in _OPEN_STOP),
                key=lambda p: p.sequence_index if p.sequence_index is not None else 0,
            )
            if not open_points:
                continue
            here = GeoPoint(courier.current_lat, courier.current_lng)
            locked_first = record.status == RouteStatus.IN_PROGRESS
            stops = [
                Stop(str(p.id), GeoPoint(p.lat, p.lng), locked=locked_first and i == 0)
                for i, p in enumerate(open_points)
            ]
            built.append(
                Route(str(record.id), CourierState(str(courier.id), here), stops)
            )
        return _FleetSnapshot(Fleet(built), skipped)


class LiveOptimizationService:
    """Computes (and briefly caches) a :class:`FleetProposal` for the active routes."""

    def __init__(
        self,
        matrix_provider: MatrixProvider,
        route_provider: GeometryProvider,
        *,
        cache: TtlCache[str, dict[str, Any]] | None = None,
    ) -> None:
        self._matrix_provider = matrix_provider
        self._route_provider = route_provider
        self._cache = cache if cache is not None else _SHARED_CACHE
        self._builder = FleetSnapshotBuilder()

    async def snapshot(self, session: AsyncSession) -> dict[str, Any]:
        records = await RouteRepository(session).list_routes(courier_id=None, status=None)
        built = self._builder.build(records)
        fleet = built.fleet
        if len(fleet) == 0:
            return self._envelope(None, [], built.skipped, "none", cached=False)
        key = self._state_key(fleet)
        cached = self._cache.get(key)
        if cached is not None:
            return {**cached, "cached": True, "skippedRoutes": built.skipped}
        index = fleet.index()
        coordinates: list[Coordinate] = [point.as_lng_lat() for point in index.points]
        started = datetime.now(UTC)
        result = await self._matrix_provider.get_duration_matrix(coordinates)
        matrix = CostMatrix(result["durations_minutes"])
        proposal = await asyncio.to_thread(LiveFleetReoptimizer(fleet, matrix).propose)
        geometries = await self._geometries(fleet, proposal)
        payload = self._envelope(
            proposal, geometries, built.skipped, str(result.get("source", "osrm")), cached=False
        )
        next_stops = NextStopFinder(fleet, matrix).find()
        explanations = LiveExplainer().build(proposal, next_stops)
        payload["explanationSource"] = await ExplanationPhraser().phrase(explanations)
        payload["nextStops"] = [item.as_json() for item in next_stops]
        payload["focusRouteId"] = next((i.route_id for i in next_stops if i.focus), None)
        payload["explanations"] = [item.as_json() for item in explanations]
        payload["computeMs"] = round((datetime.now(UTC) - started).total_seconds() * 1000)
        self._cache.put(key, payload)
        return payload

    @staticmethod
    def _state_key(fleet: Fleet) -> str:
        parts = [
            [
                route.route_id,
                [round(route.courier.location.lat, 3), round(route.courier.location.lng, 3)],
                route.stop_ids,
                route.locked_count,
            ]
            for route in fleet.routes
        ]
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    async def _geometries(
        self, fleet: Fleet, proposal: FleetProposal
    ) -> list[GeoJsonLineString | None]:
        stops = {stop.stop_id: stop for route in fleet.routes for stop in route.stops}
        out: list[GeoJsonLineString | None] = []
        drawn = 0
        for route_proposal in proposal.routes:
            worth = route_proposal.changed and (
                route_proposal.saved_minutes >= MIN_GEOMETRY_GAIN_MINUTES
                or route_proposal.moved_in
                or route_proposal.moved_out
            )
            if not worth or drawn >= MAX_GEOMETRY_ROUTES:
                out.append(None)
                continue
            courier = fleet.route(route_proposal.route_id).courier.location
            coordinates: list[Coordinate] = [courier.as_lng_lat()] + [
                stops[stop_id].location.as_lng_lat() for stop_id in route_proposal.proposed_order
            ]
            try:
                drawn += 1
                road = await self._route_provider.get_route(coordinates)
                out.append(road["geometry"])
            except (RoutingClientError, ValueError, TimeoutError) as error:
                _LOGGER.info(
                    "Live proposal geometry unavailable", extra={"error_type": type(error).__name__}
                )
                out.append(None)
        return out

    @staticmethod
    def _envelope(
        proposal: FleetProposal | None,
        geometries: list[GeoJsonLineString | None],
        skipped: list[dict[str, str]],
        source: str,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        routes: list[dict[str, Any]] = []
        if proposal is not None:
            for item, geometry in zip(proposal.routes, geometries, strict=True):
                routes.append(_route_json(item, geometry))
        return {
            "enabled": True,
            "tickSeconds": TICK_SECONDS,
            "computedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "matrixSource": source,
            "approximate": source not in {"osrm", "ors"},
            "cached": cached,
            "routes": routes,
            "moves": [
                {
                    "stopId": move.stop_id,
                    "fromRouteId": move.from_route,
                    "toRouteId": move.to_route,
                    "gainMinutes": round(move.gain_minutes, 1),
                }
                for move in (proposal.moves if proposal else ())
            ],
            "scoreBefore": proposal.score_before.as_dict() if proposal else None,
            "scoreAfter": proposal.score_after.as_dict() if proposal else None,
            "savedMinutes": round(proposal.saved_minutes, 1) if proposal else 0.0,
            "changed": bool(proposal and proposal.changed),
            "skippedRoutes": skipped,
            "nextStops": [],
            "focusRouteId": None,
            "explanations": [],
            "explanationSource": "deterministic",
        }


def _route_json(item: RouteProposal, geometry: GeoJsonLineString | None) -> dict[str, Any]:
    return {
        "routeId": item.route_id,
        "changed": item.changed,
        "currentOrder": list(item.current_order),
        "proposedOrder": list(item.proposed_order),
        "movedIn": list(item.moved_in),
        "movedOut": list(item.moved_out),
        "currentMinutes": _round(item.current_minutes),
        "proposedMinutes": _round(item.proposed_minutes),
        "savedMinutes": round(item.saved_minutes, 1),
        "geometry": geometry,
    }


def _round(value: float) -> float | None:
    return round(value, 1) if value == value and value != float("inf") else None


_SHARED_CACHE: TtlCache[str, dict[str, Any]] = TtlCache(64, TICK_SECONDS)
