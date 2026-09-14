"""Operación privada de repartidores, credenciales de acceso y telemetría GPS."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse, Response

from app.api.auth import require_courier_session, require_dispatcher_session
from app.core.auth import SessionClaims, create_courier_access_code, hash_courier_access_code
from app.core.location.courier_location import COURIER_LOCATION_MAX_AGE, is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.passwords import hash_password
from app.db.session import get_session, get_session_factory
from app.models import (
    Account,
    Courier,
    CourierCredential,
    CourierStatus,
    LocationEvent,
)
from app.repositories.courier_repository import CourierRepository
from app.schemas.couriers import (
    CourierCollectionResponse,
    CourierCreatedResponse,
    CourierCreateRequest,
    CourierLocationInput,
    CourierResponse,
    CourierSelfView,
    CourierUpdateRequest,
    CourierView,
    LocationEventResponse,
    LocationEventView,
)
from app.services.customer_notifications import (
    any_customer_notification_delivery_configured,
    dispatch_pending_customer_notifications_background,
    enqueue_courier_near_notification,
    is_courier_near_delivery_point,
)
from app.services.location_event_queue import (
    LocationEventQueue,
    LocationQueueFullError,
    ShardedLocationQueue,
)

router = APIRouter(prefix="/repartidores", tags=["repartidores"])
_LOGGER = logging.getLogger(__name__)
LocationQueue = LocationEventQueue | ShardedLocationQueue
_location_queue: LocationQueue = ShardedLocationQueue()


class _LocationWriteResult(TypedDict):
    event: LocationEvent
    accepted: bool
    nearby_notification_queued: bool


def get_location_event_queue() -> LocationQueue:
    return _location_queue


def get_write_session_factory() -> async_sessionmaker[AsyncSession]:
    return get_session_factory()


@router.get("/me")
async def get_my_courier(
    claims: Annotated[SessionClaims, Depends(require_courier_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    courier_id = _parse_uuid(
        claims.get("courierId"), "El identificador del repartidor no es válido."
    )
    courier = await CourierRepository(session).get_courier(courier_id)
