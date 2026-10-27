from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.auth import _configured_secret, _dispatcher_code
from app.core.auth import create_courier_access_code, hash_courier_access_code
from app.core.passwords import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    AccessSession,
    Account,
    Courier,
    CourierCredential,
    CourierStatus,
    SecurityAuditEvent,
)
from app.services.address_advisor import get_address_ai_advisor
from app.services.address_geocoder import AddressGeocoder, get_address_geocoder
from app.settings import Settings, get_settings

DISPATCHER_CODE = "unit-test-dispatcher-access-code-32-chars"
SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


@pytest.mark.asyncio
async def test_address_resolver_accepts_text_only_after_real_login(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.post(
        "/api/v1/direcciones/resolver", json={"address": "Carrera 25 num 3"},
    )).status_code == 401
    geocoder = AddressGeocoder(api_key="fake-unit-key", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [{
            "type": "Cross Street", "position": {"lat": 1.21, "lon": -77.28},
            "address": {"countryCode": "CO", "municipality": "Pasto",
                        "freeformAddress": "Carrera 25 con Calle 3, Pasto"},
        }]}),
    ))
    app.dependency_overrides[get_address_geocoder] = lambda: geocoder
    app.dependency_overrides[get_address_ai_advisor] = lambda: None
    try:
        await client.post("/api/v1/auth/login", json={"accessCode": DISPATCHER_CODE})
        response = await client.post(
            "/api/v1/direcciones/resolver", json={"address": "carrera 25 num 3"},
        )
        assert response.status_code == 200
        assert response.json()["candidates"][0]["precision"] == "intersection"
        invalid = await client.post(
            "/api/v1/direcciones/resolver", json={"address": "x" * 161},
        )
        assert invalid.status_code == 422
    finally:
        app.dependency_overrides.pop(get_address_geocoder, None)
        app.dependency_overrides.pop(get_address_ai_advisor, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/api", "/api/v1"])
async def test_logout_revokes_cookie_replay_on_both_versions(
    client: httpx.AsyncClient, prefix: str,
) -> None:
    login = await client.post(f"{prefix}/auth/login", json={"accessCode": DISPATCHER_CODE})
    assert login.status_code == 200
    raw = client.cookies.get("rutas-pasto-dispatch-session")
    assert raw is not None and len(raw) == 43
    assert (await client.post(f"{prefix}/auth/logout")).status_code == 200
    replay = await client.get(
        f"{prefix}/auth/session", headers={"Cookie": f"rutas-pasto-dispatch-session={raw}"},
    )
