from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.core.routing.cached_matrix_provider import MatrixProviderResult
from app.core.routing.contracts import Coordinate, RouteResult
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    Route,
    RouteGeometryProvider,
    RouteRevision,
    RouteStatus,
)
from app.repositories.customer_notification_repository import CustomerNotificationRepository
from app.services.customer_notifications import (
    enqueue_courier_near_notification,
    enqueue_order_assigned_notification,
)
from app.services.route_planner import (
    OptimizedRoutePlan,
    RouteConflictError,
    create_optimized_routes_batch,
    get_routing_providers,
    recalculate_route,
    undo_route_recalculation,
    update_route_status,
)
from app.services.route_traffic_advisor import get_route_traffic_advisor
from app.services.traffic import get_traffic_service
from app.settings import get_settings

_INTEGRATION_SECRET = "postgres-integration-secret-with-at-least-32-bytes"
_COURIER_ID = uuid4()
_POINT_ID = uuid4()
_COMPETING_COURIER_ID = uuid4()
_FIRST_POINT_ID = uuid4()
_CONFLICTING_POINT_ID = uuid4()
_EXISTING_ROUTE_ID = uuid4()
_CANCELLATION_COURIER_ID = uuid4()
_CANCELLATION_ROUTE_ID = uuid4()
_DELIVERED_POINT_ID = uuid4()
_ACTIVE_POINT_ID = uuid4()


class _MatrixProvider:
    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> MatrixProviderResult:
        size = len(coordinates)
        return {
            "durations_minutes": [
                [0.0 if row == column else 4.5 for column in range(size)]
                for row in range(size)
            ],
            "distances_meters": [
                [0.0 if row == column else 650.0 for column in range(size)]
                for row in range(size)
            ],
