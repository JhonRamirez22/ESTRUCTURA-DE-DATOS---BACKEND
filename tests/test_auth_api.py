from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
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
from app.models import AccessSession, Account, Courier, CourierCredential, CourierStatus
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
    raw = client.cookies.get("rutas-pasto-session")
    assert raw is not None and len(raw) == 43
    assert (await client.post(f"{prefix}/auth/logout")).status_code == 200
    replay = await client.get(
        f"{prefix}/auth/session", headers={"Cookie": f"rutas-pasto-session={raw}"},
    )
    assert replay.json() == {"authenticated": False}


@pytest.mark.asyncio
async def test_mutation_rejects_missing_csrf_and_foreign_origin(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/auth/login", json={"accessCode": DISPATCHER_CODE})
    for headers in (
        {"X-CSRF-Token": ""},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = await client.post("/api/v1/auth/logout", headers=headers)
        assert response.status_code == 403
    assert (await client.get("/api/v1/auth/session")).json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_old_stateless_cookie_no_longer_authorizes(client: httpx.AsyncClient) -> None:
    from time import time

    from app.core.auth import create_session_token

    token = create_session_token("dispatcher", int(time()) + 3600, SESSION_SECRET)
    response = await client.get(
        "/api/v1/auth/session", headers={"Cookie": f"rutas-pasto-session={token}"},
    )
    assert response.json() == {"authenticated": False}


@pytest.mark.asyncio
async def test_account_disable_is_checked_after_login(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await client.post("/api/v1/auth/login", json={"accessCode": DISPATCHER_CODE})
    async with session_factory() as session:
        owner = await session.scalar(select(Account))
        assert owner is not None
        owner.enabled = False
        await session.commit()
    assert (await client.get("/api/v1/auth/session")).json() == {"authenticated": False}
    async with session_factory() as session:
        stored = await session.scalar(select(AccessSession))
        assert stored is not None and stored.revoked_at is not None


@pytest.mark.asyncio
async def test_security_headers_request_id_and_body_limit(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/session", headers={"X-Request-ID": "malformed"})
    UUID(response.headers["X-Request-ID"])
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    oversized = await client.post("/api/v1/auth/login", content="x" * 65537)
    assert oversized.status_code == 413

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 32768
        yield b"x" * 32769

    streamed = await client.post("/api/v1/auth/login", content=chunks())
    assert streamed.status_code == 413
    legacy = await client.get("/api/auth/session")
    assert legacy.headers["X-API-Deprecated"] == "true"


def test_missing_environment_defaults_to_production_without_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("NODE_ENV", "AUTH_SECRET", "DISPATCHER_ACCESS_CODE"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)

    assert settings.node_env == "production"
    assert not settings.is_development
    assert _configured_secret(settings) is None
    assert _dispatcher_code(settings) is None


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
