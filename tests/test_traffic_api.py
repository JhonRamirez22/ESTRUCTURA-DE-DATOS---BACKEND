from __future__ import annotations

from datetime import UTC, datetime
from time import time

import httpx
import pytest
from fastapi import FastAPI

from app.api.auth import get_optional_session
from app.api.traffic import router
from app.core.auth import SESSION_MAX_AGE_SECONDS, create_session_token, session_cookie_name
from app.core.traffic.tomtom import TrafficData, TrafficLevel
from app.services.traffic import get_traffic_service
from app.settings import get_settings
from tests.auth_helpers import business_identity

_SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


class FakeTrafficService:
    async def get_flow_segment(self, _coordinate: tuple[float, float]) -> TrafficData:
        return TrafficData(
            current_speed_kmh=18,
            free_flow_speed_kmh=40,
            current_travel_time_seconds=120,
            free_flow_travel_time_seconds=60,
            confidence=0.9,
            road_closure=False,
            traffic_ratio=0.45,
            traffic_level=TrafficLevel.ALTO,
            travel_time_multiplier=2.0,
            timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        )


def _cookie(role: str = "dispatcher") -> str:
    token = create_session_token(
        role,  # type: ignore[arg-type]
        int(time()) + SESSION_MAX_AGE_SECONDS,
        _SESSION_SECRET,
        "courier-test-id" if role == "courier" else None,
    )
    return f"{session_cookie_name(role)}={token}"


@pytest.mark.asyncio
async def test_traffic_api_is_authenticated_and_returns_units_and_cache_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _SESSION_SECRET)
    get_settings.cache_clear()
    application = FastAPI()
    application.dependency_overrides[get_optional_session] = business_identity
    application.include_router(router, prefix="/api")
    application.dependency_overrides[get_traffic_service] = lambda: FakeTrafficService()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            unauthorized = await client.get("/api/traffic?lat=1.2136&lon=-77.2811")
            response = await client.get(
                "/api/traffic?lat=1.2136&lon=-77.2811",
                headers={"Cookie": _cookie()},
            )
    finally:
        application.dependency_overrides.clear()
        get_settings.cache_clear()

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "currentSpeedKmh": 18,
        "freeFlowSpeedKmh": 40,
        "currentTravelTimeSeconds": 120,
        "freeFlowTravelTimeSeconds": 60,
        "confidence": 0.9,
        "roadClosure": False,
        "trafficRatio": 0.45,
        "travelTimeMultiplier": 2.0,
        "trafficLevel": "ALTO",
        "timestamp": "2026-10-03T00:00:00+00:00",
        "cacheStatus": "LIVE",
    }
    assert response.headers["cache-control"] == "private, no-store, max-age=0"


@pytest.mark.asyncio
async def test_traffic_api_rejects_coordinates_outside_pasto_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _SESSION_SECRET)
    get_settings.cache_clear()
    application = FastAPI()
    application.dependency_overrides[get_optional_session] = business_identity
    application.include_router(router, prefix="/api")
    fake_service = FakeTrafficService()
    application.dependency_overrides[get_traffic_service] = lambda: fake_service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/traffic?lat=4&lon=-70",
                headers={"Cookie": _cookie("courier")},
            )
    finally:
        application.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 400
    assert "solo está disponible dentro de Pasto" in response.json()["error"]


@pytest.mark.asyncio
async def test_traffic_api_explains_missing_key_without_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET", _SESSION_SECRET)
    get_settings.cache_clear()
    application = FastAPI()
    application.dependency_overrides[get_optional_session] = business_identity
    application.include_router(router, prefix="/api")
    application.dependency_overrides[get_traffic_service] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/traffic?lat=1.2136&lon=-77.2811",
                headers={"Cookie": _cookie()},
            )
    finally:
        application.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 503
    assert "no está configurado" in response.json()["error"]
