"""Live fleet re-optimisation endpoint: permissions, proposals, caching and failure modes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection
from time import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import optimizations as optimizations_module
from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.core.location.proximity import haversine_m
from app.core.routing.contracts import Coordinate
from app.core.routing.routing_errors import RoutingClientError
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Route,
    RouteStatus,
)
from app.services import live_optimization
from app.services.route_planner import get_routing_providers
from app.settings import get_settings

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


class FakeMatrix:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RoutingClientError("osrm down")
        rows = [
            [0.0 if a == b else haversine_m(a[1], a[0], b[1], b[0]) / 400.0 for b in coordinates]
            for a in coordinates
        ]
        return {"durations_minutes": rows, "source": "osrm"}


class FakeRoutes:
    def __init__(self) -> None:
        self.calls = 0

    async def get_route(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        self.calls += 1
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
