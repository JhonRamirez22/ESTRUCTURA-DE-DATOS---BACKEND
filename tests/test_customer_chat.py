from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.routing.contracts import Coordinate, RouteResult
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    Courier,
    CourierStatus,
    CustomerChatRateLimitWindow,
    DeliveryPoint,
    DeliveryPointStatus,
    Route,
    RouteStatus,
)
from app.schemas.customer_chat import CustomerChatTurn
from app.services.customer_order_chat import (
    CustomerChatProviderError,
    CustomerChatRateLimitError,
    CustomerChatTimeoutError,
    HttpOrderChatAssistant,
    OrderChatAssistant,
    build_order_fallback_answer,
    create_customer_order_chat,
    customer_asks_for_arrival_estimate,
    get_customer_order_chat,
)
from app.services.route_planner import get_routing_providers
from app.services.traffic import get_traffic_service
from app.settings import Settings, get_settings


class _FakeAssistant:
    def __init__(self, reply: str = "Tu pedido está registrado.") -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def answer(
        self,
        *,
        delivery_status: str,
        route_status: str | None,
        messages: Sequence[CustomerChatTurn],
        estimated_minutes_from_now: float | None = None,
        estimate_basis: str = "",
    ) -> str:
        self.calls.append(
            {
                "delivery_status": delivery_status,
                "route_status": route_status,
                "estimated_minutes_from_now": estimated_minutes_from_now,
                "estimate_basis": estimate_basis,
                "messages": messages,
            }
        )
        return self.reply


class _FailingAssistant:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def answer(
        self,
        *,
        delivery_status: str,
        route_status: str | None,
        messages: Sequence[CustomerChatTurn],
        estimated_minutes_from_now: float | None = None,
        estimate_basis: str = "",
    ) -> str:
