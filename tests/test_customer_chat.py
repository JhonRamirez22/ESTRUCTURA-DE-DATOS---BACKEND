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
from app.settings import Settings


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
        del estimate_basis
        raise self.error


class _FakeMatrixProvider:
    def __init__(self, *, fails: bool = False, source: str = "osrm") -> None:
        self.coordinates: list[list[Coordinate]] = []
        self.fails = fails
        self.source = source

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> dict[str, object]:
        self.coordinates.append(list(coordinates))
        if self.fails:
            raise RuntimeError("simulated routing outage")
        size = len(coordinates)
        durations: list[list[float | None]] = [
            [0.0 if source == target else 11.0 for target in range(size)]
            for source in range(size)
        ]
        distances: list[list[float | None]] = [
            [0.0 if source == target else 1_000.0 for target in range(size)]
            for source in range(size)
        ]
        return {
            "durations_minutes": durations,
            "distances_meters": distances,
            "source": self.source,
        }


class _FakeRouteProvider:
    def __init__(self, *, fails: bool = False) -> None:
        self.coordinates: list[list[Coordinate]] = []
        self.fails = fails

    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        self.coordinates.append(list(coordinates))
        if self.fails:
            raise RuntimeError("simulated directions outage")
        return {
            "geometry": {"type": "LineString", "coordinates": list(coordinates)},
            "geometry_provider": "OSRM",
            "distance_meters": 825,
            "duration_minutes": 7.5,
        }


class _FakeTrafficProvider:
    async def get_flow_segment(self, coordinate: Coordinate) -> TrafficData:
        del coordinate
        return TrafficData(
            current_speed_kmh=20,
            free_flow_speed_kmh=40,
            current_travel_time_seconds=90,
            free_flow_travel_time_seconds=60,
            confidence=0.9,
            road_closure=False,
            traffic_ratio=0.5,
            traffic_level=TrafficLevel.ALTO,
            travel_time_multiplier=1.5,
            timestamp=datetime.now(UTC),
            cache_status="LIVE",
        )


def test_customer_chat_uses_fast_groq_defaults_when_only_a_key_is_supplied() -> None:
    assistant = create_customer_order_chat(Settings(customer_chat_api_key="test-only-key"))

    assert isinstance(assistant, HttpOrderChatAssistant)
    assert assistant._api_url == "https://api.groq.com/openai/v1/chat/completions"
    assert assistant._model == "openai/gpt-oss-20b"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'customer-chat.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    routing_providers: tuple[_FakeMatrixProvider, _FakeRouteProvider],
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_routing_providers] = lambda: routing_providers
    app.dependency_overrides[get_traffic_service] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_routing_providers, None)
        app.dependency_overrides.pop(get_traffic_service, None)
        app.dependency_overrides.pop(get_customer_order_chat, None)


@pytest.fixture
def routing_providers() -> tuple[_FakeMatrixProvider, _FakeRouteProvider]:
    return _FakeMatrixProvider(), _FakeRouteProvider()


async def _create_order(
    session_factory: async_sessionmaker[AsyncSession], guide: str = "secret-guide-123"
) -> None:
    async with session_factory() as session:
        session.add(
            DeliveryPoint(
                id=uuid4(),
                address="Carrera 25 # 4 sur 65",
                lat=1.2136,
                lng=-77.2811,
                status=DeliveryPointStatus.PENDING,
                order_id="internal-order-id",
                tracking_token=guide,
            )
        )
        await session.commit()


async def _create_active_order(
    session_factory: async_sessionmaker[AsyncSession],
    guide: str,
    *,
    location_at: datetime | None = None,
) -> None:
    now = location_at or datetime.now(UTC).replace(tzinfo=None)
    courier = Courier(
        id=uuid4(),
        name="Repartidor privado",
        phone="+573001234567",
        status=CourierStatus.ON_ROUTE,
        current_lat=1.213612,
        current_lng=-77.281145,
        last_location_at=now,
    )
    route = Route(
        id=uuid4(),
        courier_id=courier.id,
        status=RouteStatus.IN_PROGRESS,
        estimated_duration_minutes=25,
        estimated_distance_meters=2_000,
    )
    point = DeliveryPoint(
        id=uuid4(),
        address="Dirección privada del cliente",
        lat=1.215,
        lng=-77.279,
        status=DeliveryPointStatus.EN_ROUTE,
        order_id=f"internal-{uuid4()}",
        tracking_token=guide,
        route_id=route.id,
        sequence_index=0,
    )
    async with session_factory() as session:
        session.add_all([courier, route, point])
        await session.commit()


@pytest.mark.asyncio
async def test_http_assistant_sends_only_minimal_order_context_to_openai_compatible_api() -> None:
    captured: dict[str, object] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Tu pedido está registrado."}}]},
        )

    assistant = HttpOrderChatAssistant(
        api_url="https://models.example/v1/chat/completions",
        api_key="private-test-key",
