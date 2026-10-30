"""Solicitudes de clientes: entran en espera, despacho las resuelve y nunca se asignan solas."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_orders_api import _cookie, client, session_factory  # noqa: F401

from app.api import order_requests
from app.models import DeliveryPoint, DeliveryPointStatus

_ORIGIN = {"Origin": "http://test"}
_VALID = {"address": "Calle 18 # 22-14, Centro", "lat": 1.2136, "lng": -77.2811}


@pytest.fixture(autouse=True)
def _relaxed_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cada prueba usa una base nueva; los límites se prueban por separado.
    monkeypatch.setattr(order_requests, "_CLIENT_LIMITER", order_requests.CustomerChatRateLimiter(
        max_requests=100, window_seconds=600
    ))


async def _create(client: httpx.AsyncClient, **headers: str) -> httpx.Response:  # noqa: F811
    return await client.post("/api/v1/solicitudes", headers={**_ORIGIN, **headers}, json=_VALID)


@pytest.mark.asyncio
async def test_public_request_waits_and_dispatcher_accepts_it(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    created = await _create(client)
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "REQUESTED" and body["reference"].startswith("SOL-")
    guide = body["trackingGuide"]

    tracking = await client.get("/api/v1/seguimiento", params={"guia": guide})
    assert tracking.json()["tracking"]["status"] == "REQUESTED"

    dispatcher = {"Cookie": _cookie("dispatcher")}
    pending = await client.get("/api/v1/pedidos", params={"status": "PENDING"}, headers=dispatcher)
    assert pending.json()["deliveryPoints"] == []
    requested = await client.get(
        "/api/v1/pedidos", params={"status": "REQUESTED"}, headers=dispatcher
    )
    request_id = requested.json()["deliveryPoints"][0]["id"]

    accepted = await client.post(f"/api/v1/solicitudes/{request_id}/aceptar", headers=dispatcher)
    assert accepted.status_code == 200
    again = await client.post(f"/api/v1/solicitudes/{request_id}/aceptar", headers=dispatcher)
    assert again.status_code == 409
    async with session_factory() as session:
        point = await session.get(DeliveryPoint, UUID(request_id))
        assert point is not None and point.status is DeliveryPointStatus.PENDING


@pytest.mark.asyncio
async def test_dispatcher_rejects_request_and_guide_stops_working(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    guide = (await _create(client)).json()["trackingGuide"]
    async with session_factory() as session:
        point = await session.scalar(select(DeliveryPoint))
        assert point is not None
    dispatcher = {"Cookie": _cookie("dispatcher")}
    rejected = await client.post(f"/api/v1/solicitudes/{point.id}/rechazar", headers=dispatcher)
