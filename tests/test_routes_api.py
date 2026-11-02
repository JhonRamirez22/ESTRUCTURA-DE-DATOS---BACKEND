from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from time import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.core.ai.route_traffic import RouteTrafficAdjustment, RouteTrafficAdvice
from app.core.auth import (
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    session_cookie_name,
)
from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteGeometryProvider,
    RouteStatus,
)
from app.services.route_planner import (
    OptimizedRoutePlan,
    RouteConflictError,
    create_optimized_routes_batch,
    get_routing_providers,
    recalculate_route,
)
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"
_COURIER_ID = UUID("f8fbc95f-643e-4fb3-9b82-8c42e0707311")


class FakeMatrixProvider:
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        size = len(coordinates)
        durations: list[list[float | None]] = [
            [0.0 if row == column else 5.0 + row + column for column in range(size)]
            for row in range(size)
        ]
        distances: list[list[float | None]] = [
            [0.0 if row == column else 500.0 + row + column for column in range(size)]
            for row in range(size)
        ]
        if size >= 3:
            durations[0][1] = 8
            durations[0][2] = 2
            durations[2][1] = 1
            durations[1][2] = 1
        return {
            "durations_minutes": durations,
            "distances_meters": distances,
            "source": "osrm",
        }


class FakeRouteProvider:
    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 2_100,
            "duration_minutes": 12.5,
