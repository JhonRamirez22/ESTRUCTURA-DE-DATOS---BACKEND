from __future__ import annotations

import asyncio
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
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.ai.assignment_traffic import (
    AssignmentTrafficAdjustment,
    AssignmentTrafficAdvice,
    AssignmentTrafficRequest,
)
from app.core.ai.route_traffic import RouteTrafficAdvice
from app.core.auth import (
    SESSION_MAX_AGE_SECONDS,
    create_session_token,
    session_cookie_name,
)
from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.db.base import Base
from app.db.session import get_session, get_session_factory
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    Route,
    RouteStatus,
)
from app.services.route_planner import get_routing_providers
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


class FakeMatrixProvider:
    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, Any]:
        size = len(coordinates)
        durations = [
            [0.0 if source == target else float(3 + abs(source - target)) for target in range(size)]
            for source in range(size)
        ]
        distances = [
            [0.0 if source == target else float(300 + 100 * abs(source - target))
             for target in range(size)]
            for source in range(size)
        ]
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
            "distance_meters": 1_600,
            "duration_minutes": 11.5,
        }


class FakeTrafficAdvisor:
    def __init__(self, *, fail_route_advice: bool = False) -> None:
        self.assignment_request: AssignmentTrafficRequest | None = None
        self.route_requests = 0
