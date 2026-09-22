"""Planificación y persistencia atómica de rutas optimizadas."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.route_traffic import (
    RouteTrafficAiRequest,
    apply_route_traffic_adjustments,
    build_route_traffic_candidates,
    filter_route_traffic_adjustments,
    select_route_traffic_candidates,
)
from app.core.location.courier_location import is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.or_opt import or_opt
from app.core.optimization.order_verifier import verify_candidate_order
from app.core.optimization.refine import RefineResult, refine_route
from app.core.optimization.time_windows import (
    Window,
    WindowEvaluation,
    plan_with_windows,
    window_from_json,
)
from app.core.optimization.two_opt import two_opt
from app.core.routing.cached_matrix_provider import (
    CachedMatrixProvider,
    MatrixClient,
    MatrixProviderResult,
    MatrixSource,
)
from app.core.routing.cached_route_provider import CachedRouteProvider, RouteClient
from app.core.routing.contracts import (
    Coordinate,
    GeoJsonLineString,
    GeometryProvider,
    RouteResult,
)
from app.core.routing.ors_client import OrsClient
from app.core.routing.osrm_client import OsrmClient
from app.core.structures.lru_cache import LruCache
from app.core.structures.stack import Stack
from app.core.traffic.route_sampling import sample_route_leg_points
from app.core.traffic.tomtom import TrafficData
from app.models import (
    Courier,
    CourierStatus,
    CustomerNotificationChannel,
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    NotificationKind,
    Route,
    RouteGeometryProvider,
    RouteRevision,
    RouteStatus,
    TrafficObservation,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.route_repository import RouteRepository
from app.services.customer_notifications import enqueue_order_assigned_notification
from app.services.delivery_events import SYSTEM_ACTOR, DeliveryEventRecorder, EventActor
from app.services.route_order_advisor import MAX_AI_ORDER_STOPS, RouteOrderAdvisor
from app.services.route_traffic_advisor import RouteTrafficAdvisor
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
MAX_ROUTE_STOPS = 50
MAX_ROUTE_REVISIONS = 20
MAX_ROUTE_TRAFFIC_AI_CANDIDATES = 32
_ACTIVE_ROUTE_STATUSES = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)
_ACTIVE_POINT_STATUSES = (DeliveryPointStatus.PENDING, DeliveryPointStatus.EN_ROUTE)
_TERMINAL_POINT_STATUSES = (DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED)
