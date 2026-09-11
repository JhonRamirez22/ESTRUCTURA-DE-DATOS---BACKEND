"""Punto de entrada de la API Python; el frontend conserva Next.js y TypeScript."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.addresses import router as addresses_router
from app.api.analytics import router as analytics_router
from app.api.assignments import router as assignments_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.couriers import get_location_event_queue
from app.api.couriers import router as couriers_router
from app.api.customer_chat import router as customer_chat_router
from app.api.demo import router as demo_router
from app.api.geocoding import router as geocoding_router
from app.api.health import router as health_router
from app.api.history import router as history_router
from app.api.intelligence import router as intelligence_router
from app.api.notifications import router as notifications_router
from app.api.optimizations import router as optimizations_router
from app.api.order_requests import router as order_requests_router
from app.api.orders import router as orders_router
from app.api.routes import router as routes_router
from app.api.tracking import router as tracking_router
from app.api.traffic import router as traffic_router
from app.api.traffic_tiles import router as traffic_tiles_router
from app.db.session import dispose_engine, get_session_factory
from app.services.customer_notification_worker import run_customer_notification_worker
from app.services.customer_notifications import any_customer_notification_delivery_configured
from app.services.http_security import HttpSecurityMiddleware
from app.services.route_planner import close_routing_clients
from app.services.traffic import close_traffic_service
from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Los proveedores usan claves en query: httpx INFO registraría esas URLs completas.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    notification_stop = asyncio.Event()
    notification_worker: asyncio.Task[None] | None = None
    _app.state.customer_notification_worker_task = None
