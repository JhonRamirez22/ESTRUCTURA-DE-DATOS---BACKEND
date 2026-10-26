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
    assert rejected.status_code == 200
    gone = await client.get("/api/v1/seguimiento", params={"guia": guide})
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_only_dispatchers_resolve_requests_and_requests_cannot_be_patched(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _create(client)
    async with session_factory() as session:
        point = await session.scalar(select(DeliveryPoint))
        assert point is not None
    anonymous = await client.post(f"/api/v1/solicitudes/{point.id}/aceptar")
    assert anonymous.status_code == 401
    courier = {"Cookie": _cookie("courier", "f8fbc95f-643e-4fb3-9b82-8c42e0707311")}
    as_courier = await client.post(f"/api/v1/solicitudes/{point.id}/aceptar", headers=courier)
    assert as_courier.status_code == 403
    patched = await client.patch(
        f"/api/v1/pedidos/{point.id}",
        headers={"Cookie": _cookie("dispatcher")},
        json={"status": "PENDING"},
    )
    assert patched.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "payload", "expected"),
    [
        ({}, _VALID, 403),  # sin origen
        ({"Origin": "https://evil.example"}, _VALID, 403),
        (_ORIGIN, {**_VALID, "lat": 4.711, "lng": -74.07}, 400),  # Bogotá
        (_ORIGIN, {**_VALID, "address": "abc"}, 400),
        (_ORIGIN, {**_VALID, "customerPhone": "3001112233"}, 400),  # datos no pedidos
        (_ORIGIN, {**_VALID, "address": "<script>alert(1)</script> calle 5"}, 400),
    ],
)
async def test_public_request_rejects_unsafe_or_foreign_input(
    client: httpx.AsyncClient,  # noqa: F811
    headers: dict[str, str],
    payload: dict[str, object],
    expected: int,
) -> None:
    response = await client.post("/api/v1/solicitudes", headers=headers, json=payload)
    assert response.status_code == expected


@pytest.mark.asyncio
async def test_public_requests_are_rate_limited_and_capped(
    client: httpx.AsyncClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(order_requests, "_CLIENT_LIMITER", order_requests.CustomerChatRateLimiter(
        max_requests=2, window_seconds=600
    ))
    statuses = [(await _create(client, **{"X-Forwarded-For": "203.0.113.7"})).status_code
                for _ in range(3)]
    assert statuses == [201, 201, 429]

    monkeypatch.setattr(order_requests, "MAX_WAITING_REQUESTS", 2)
    capped = await _create(client, **{"X-Forwarded-For": "198.51.100.9"})
    assert capped.status_code == 503


class _FakeGeocoder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, address: str) -> list[object]:
        from app.services.address_geocoder import AddressSuggestion

        self.queries.append(address)
        return [AddressSuggestion(address="Calle 18 # 22-14, Pasto", lat=1.2136, lng=-77.2811,
                                  precision="address")]


@pytest.mark.asyncio
async def test_public_address_lookup_requires_origin_and_caches_results(
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    from app.main import app
    from app.services.address_advisor import get_address_ai_advisor
    from app.services.address_geocoder import get_address_geocoder

    order_requests._LOOKUP_CACHE.clear()
    geocoder = _FakeGeocoder()
    app.dependency_overrides[get_address_geocoder] = lambda: geocoder
    app.dependency_overrides[get_address_ai_advisor] = lambda: None
    try:
        no_origin = await client.post(
            "/api/v1/solicitudes/direccion", json={"address": "calle 18 22 14"}
        )
        assert no_origin.status_code == 403
        first = await client.post(
            "/api/v1/solicitudes/direccion", headers=_ORIGIN, json={"address": "calle 18 # 22-14"}
        )
        assert first.status_code == 200
        assert first.json()["candidates"][0]["lat"] == 1.2136
        assert first.json()["cached"] is False
        again = await client.post(
            "/api/v1/solicitudes/direccion", headers=_ORIGIN, json={"address": "Calle 18 # 22-14"}
        )
        assert again.json()["cached"] is True
        assert len(geocoder.queries) == 1
        foreign = await client.post(
            "/api/v1/solicitudes/direccion", headers=_ORIGIN, json={"address": "calle 5 bogota"}
        )
        assert foreign.json()["candidates"] == []
        markup = await client.post(
            "/api/v1/solicitudes/direccion", headers=_ORIGIN, json={"address": "<b>calle 5</b>"}
        )
        assert markup.status_code == 400
    finally:
        app.dependency_overrides.pop(get_address_geocoder, None)
        app.dependency_overrides.pop(get_address_ai_advisor, None)


@pytest.mark.asyncio
async def test_request_near_a_scheduled_stop_gets_a_private_notice(
    client: httpx.AsyncClient,  # noqa: F811
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    async with session_factory() as session:
        session.add(
            DeliveryPoint(
                id=uuid4(), address="Calle 18 # 22-14", lat=1.2136, lng=-77.2811,
                status=DeliveryPointStatus.PENDING, order_id="DEMO-1",
                tracking_token=str(uuid4()),
            )
        )
        await session.commit()
    near = await client.post(
        "/api/v1/solicitudes", headers=_ORIGIN,
        json={"address": "Calle 18 # 22-20 Centro", "lat": 1.21365, "lng": -77.2811},
    )
    assert near.status_code == 201
    notice = near.json()["nearbyStop"]
    assert notice["status"] == "PENDING" and notice["distanceMeters"] < 150
    assert "DEMO-1" not in near.text and "Calle 18" not in near.text
    far = await client.post(
        "/api/v1/solicitudes", headers={**_ORIGIN, "X-Forwarded-For": "198.51.100.44"},
        json={"address": "Carrera 40 # 19-02 Anganoy", "lat": 1.2300, "lng": -77.2900},
    )
    assert far.json()["nearbyStop"] is None
