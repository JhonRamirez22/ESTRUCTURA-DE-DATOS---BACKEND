from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    Route,
    RouteStatus,
)
from app.services.route_planner import get_routing_providers
from app.services.traffic import get_traffic_service

_COURIER_ID = UUID("b14a9c80-e0ae-4f7f-a286-1f5622fcacb3")


class FakeMatrixProvider:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls = 0

    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> dict[str, Any]:
        self.calls += 1
        if self.fails:
            raise RuntimeError("matrix unavailable")
        size = len(coordinates)
        durations = [[0.0 if i == j else 8.0 for j in range(size)] for i in range(size)]
        distances = [[0.0 if i == j else 1_200.0 for j in range(size)] for i in range(size)]
        if size >= 3:
            durations[1][2] = 6.0
            distances[1][2] = 700.0
        return {
            "durations_minutes": durations,
            "distances_meters": distances,
            "source": "osrm",
        }


class FakeRouteProvider:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list[list[Coordinate]] = []

    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        self.calls.append(list(coordinates))
        if self.fails:
            raise RuntimeError("route unavailable")
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 1_450,
            "duration_minutes": 9.0,
        }


