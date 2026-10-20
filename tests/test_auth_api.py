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
    raw = client.cookies.get("rutas-pasto-dispatch-session")
    assert raw is not None and len(raw) == 43
    assert (await client.post(f"{prefix}/auth/logout")).status_code == 200
    replay = await client.get(
        f"{prefix}/auth/session", headers={"Cookie": f"rutas-pasto-dispatch-session={raw}"},
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
        "/api/v1/auth/session", headers={"Cookie": f"rutas-pasto-dispatch-session={token}"},
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
    https_response = await client.get(
        "/api/v1/auth/session", headers={"X-Forwarded-Proto": "https"}
    )
    assert https_response.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )
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
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", SESSION_SECRET)
    monkeypatch.setenv("DISPATCHER_ACCESS_CODE", DISPATCHER_CODE)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.setenv("AUTH_LEGACY_CODE_LOGIN_ENABLED", "true")
    get_settings.cache_clear()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Origin": "http://test"},
        ) as test_client:
            async def remember_csrf(response: httpx.Response) -> None:
                proof = response.headers.get("X-CSRF-Token")
                if proof:
                    test_client.headers["X-CSRF-Token"] = proof

            test_client.event_hooks["response"].append(remember_csrf)
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_login_rejects_invalid_json_and_non_object_body(
    client: httpx.AsyncClient,
) -> None:
    malformed = await client.post("/api/auth/login", content="{")
    null_body = await client.post(
        "/api/auth/login", content="null", headers={"Content-Type": "application/json"}
    )

    assert malformed.status_code == 400
    assert malformed.json() == {"error": "El body debe ser JSON válido."}
    assert null_body.status_code == 400
    assert null_body.json() == {"error": "El body debe ser un objeto JSON."}


@pytest.mark.asyncio
async def test_dispatcher_login_sets_private_cookie_and_does_not_disclose_claims(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/login",
        json={
            "role": "courier",
            "accessCode": DISPATCHER_CODE,
            "returnTo": "/repartidor",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "redirectTo": "/dashboard"}
    assert "rutas-pasto-dispatch-session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Strict" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store, private"
    assert "role" not in response.json()
    assert "courierId" not in response.json()
    session_response = await client.get("/api/auth/session")
    assert session_response.json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_email_login_uses_account_role_and_private_session(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_LEGACY_CODE_LOGIN_ENABLED", "false")
    get_settings.cache_clear()
    async with session_factory() as session:
        session.add(
            Account(
                email="despacho@example.com",
                password_hash=hash_password("Una contraseña privada 2026"),
                role="dispatcher",
            )
        )
        await session.commit()
    rejected = await client.post("/api/auth/login", json={"accessCode": DISPATCHER_CODE})
    assert rejected.status_code == 400
    response = await client.post(
        "/api/auth/login",
        json={
            "email": " DESPACHO@EXAMPLE.COM ",
            "password": "Una contraseña privada 2026",
            "role": "courier",
            "returnTo": "//evil.example",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "redirectTo": "/dashboard"}
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "password" not in response.text
    for _ in range(12):
        successful = await client.post(
            "/api/auth/login",
            json={
                "email": "despacho@example.com",
                "password": "Una contraseña privada 2026",
            },
        )
        assert successful.status_code == 200


@pytest.mark.asyncio
async def test_email_login_invalid_unknown_disabled_and_rate_limit(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El tiempo restante no debe variar con la carga del equipo durante la prueba.
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime.now(UTC)
    monkeypatch.setattr("app.services.customer_chat_rate_limiter.datetime", clock)
    async with session_factory() as session:
        session.add(
            Account(
                email="disabled@example.com",
                password_hash=hash_password("Una contraseña privada 2026"),
                role="dispatcher",
                enabled=False,
            )
        )
        await session.commit()
    malformed = await client.post("/api/auth/login", json={"email": "invalid", "password": "x"})
    assert malformed.status_code == 400
    for email in ("disabled@example.com", "unknown@example.com"):
        response = await client.post(
            "/api/auth/login",
            json={
                "email": email,
                "password": "Una contraseña privada 2026",
            },
        )
        assert response.status_code == 401
        assert response.json() == {"error": "Correo o contraseña incorrectos."}
    for _ in range(9):
        await client.post("/api/auth/login", json={"email": "unknown@example.com", "password": "x"})
    limited = await client.post(
        "/api/auth/login", json={"email": "unknown@example.com", "password": "x"}
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"


@pytest.mark.asyncio
async def test_dispatcher_provisions_courier_account_and_courier_cannot_escalate(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await client.post("/api/auth/login", json={"accessCode": DISPATCHER_CODE})
    invalid = await client.put(
        "/api/auth/courier-account",
        json={
            "courierId": "invalid",
            "email": "courier@example.com",
            "password": "LEAK-ME",
        },
    )
    assert invalid.status_code == 400
    assert "LEAK-ME" not in invalid.text
    created = await client.post(
        "/api/repartidores",
        json={
            "name": "Repartidor",
            "phone": "3000000000",
            "account": {
                "email": "courier@example.com",
                "password": "Contraseña privada para reparto",
            },
        },
    )
    assert created.status_code == 201
    assert "accessCode" not in created.json()
    duplicate = await client.post(
        "/api/repartidores",
        json={
            "name": "Duplicado",
            "phone": "3000000001",
            "account": {
                "email": "courier@example.com",
                "password": "Contraseña privada para reparto",
            },
        },
    )
    assert duplicate.status_code == 409
    await client.post("/api/auth/logout")
    login = await client.post(
        "/api/auth/login",
        json={
            "email": "courier@example.com",
            "password": "Contraseña privada para reparto",
            "role": "dispatcher",
            "returnTo": "/dashboard",
        },
    )
    assert login.json()["redirectTo"] == "/repartidor"
    forbidden = await client.put(
        "/api/auth/courier-account",
        json={
            "courierId": created.json()["courier"]["id"],
            "email": "other@example.com",
            "password": "Otra contraseña privada 2026",
        },
    )
    assert forbidden.status_code == 403
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Courier)) == 1


@pytest.mark.asyncio
async def test_page_access_is_decided_by_backend_without_exposing_role(
    client: httpx.AsyncClient,
) -> None:
    anonymous_access = await client.get("/api/auth/access", params={"path": "/dashboard"})
    anonymous_login = await client.get("/api/auth/access", params={"path": "/acceso"})
    assert anonymous_access.json() == {"allowed": False, "redirectTo": "/acceso"}
    assert anonymous_login.json() == {"allowed": True, "redirectTo": None}

    await client.post("/api/auth/login", json={"accessCode": DISPATCHER_CODE})
    dispatcher_access = await client.get("/api/auth/access", params={"path": "/dashboard"})
    courier_access = await client.get("/api/auth/access", params={"path": "/repartidor"})
    access_page = await client.get("/api/auth/access", params={"path": "/acceso"})

    assert dispatcher_access.json() == {"allowed": True, "redirectTo": None}
    assert courier_access.json() == {"allowed": False, "redirectTo": "/dashboard"}
    assert access_page.json() == {"allowed": False, "redirectTo": "/dashboard"}
    assert "role" not in courier_access.json()
    assert "courierId" not in courier_access.json()


@pytest.mark.asyncio
async def test_page_access_rejects_unmanaged_paths(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/auth/access", params={"path": "/api/pedidos"})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_courier_login_verifies_database_hash_and_limits_redirect_scope(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = "f8fbc95f-643e-4fb3-9b82-8c42e0707311"
    access_code = create_courier_access_code(courier_id)
    async with session_factory() as session:
        courier = Courier(
            id=UUID(courier_id),
            name="Repartidor de prueba",
            phone="3000000000",
            status=CourierStatus.AVAILABLE,
            credential=CourierCredential(access_code_hash=hash_courier_access_code(access_code)),
        )
        session.add(courier)
        await session.commit()

    response = await client.post(
        "/api/auth/login",
        json={"accessCode": access_code, "returnTo": "/dashboard"},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "redirectTo": "/repartidor"}
    assert "courierId" not in response.json()
    assert "role" not in response.json()
    assert (await client.get("/api/auth/session")).json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_invalid_code_and_logout_preserve_existing_http_contract(
    client: httpx.AsyncClient,
) -> None:
    invalid = await client.post("/api/auth/login", json={"accessCode": "incorrecto"})
    logout = await client.post("/api/auth/logout")

    assert invalid.status_code == 401
    assert invalid.json() == {"error": "Código de acceso inválido."}
    assert logout.status_code == 200
    assert logout.json() == {"ok": True}
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert "HttpOnly" in logout.headers["set-cookie"]
    assert logout.headers["cache-control"] == "no-store, private"


@pytest.mark.asyncio
async def test_external_return_to_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"accessCode": DISPATCHER_CODE, "returnTo": "//evil.example"},
    )

    assert response.json() == {"authenticated": True, "redirectTo": "/dashboard"}


@pytest.mark.asyncio
async def test_dispatcher_and_courier_sessions_coexist_in_one_browser(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    courier_id = "a1b2c3d4-0000-4000-8000-00000000c0de"
    access_code = create_courier_access_code(courier_id)
    async with session_factory() as session:
        session.add(Courier(
            id=UUID(courier_id),
            name="Repartidor de prueba",
            phone="3000000001",
            status=CourierStatus.AVAILABLE,
            credential=CourierCredential(access_code_hash=hash_courier_access_code(access_code)),
        ))
        await session.commit()

    dispatcher = await client.post("/api/v1/auth/login", json={"accessCode": DISPATCHER_CODE})
    assert dispatcher.status_code == 200
    dispatch_cookie = client.cookies.get("rutas-pasto-dispatch-session")
    assert dispatch_cookie is not None

    courier = await client.post("/api/v1/auth/login", json={"accessCode": access_code})
    assert courier.status_code == 200
    # El inicio de sesión del repartidor no reemplaza la cookie de despacho.
    assert client.cookies.get("rutas-pasto-dispatch-session") == dispatch_cookie
    assert client.cookies.get("rutas-pasto-courier-session") is not None

    dispatcher_page = await client.get(
        "/api/v1/auth/access", params={"path": "/dashboard"},
        headers={"X-Session-Role": "dispatcher"},
    )
    courier_page = await client.get(
        "/api/v1/auth/access", params={"path": "/repartidor"},
    )
    assert dispatcher_page.json()["allowed"] is True
    assert courier_page.json()["allowed"] is True

    dispatcher_session = await client.get(
        "/api/v1/auth/session", headers={"X-Session-Role": "dispatcher"},
    )
    assert dispatcher_session.headers.get("X-Session-Role") == "dispatcher"
    assert "X-CSRF-Token" in dispatcher_session.headers

    # Cerrar la sesión de repartidor no cierra la de despacho.
    await client.post(
        "/api/v1/auth/logout",
        headers={
            "X-Session-Role": "courier",
            "X-CSRF-Token": courier.headers["X-CSRF-Token"],
        },
    )
    still_dispatcher = await client.get(
        "/api/v1/auth/session", headers={"X-Session-Role": "dispatcher"},
    )
    assert still_dispatcher.headers.get("X-Session-Role") == "dispatcher"
