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
