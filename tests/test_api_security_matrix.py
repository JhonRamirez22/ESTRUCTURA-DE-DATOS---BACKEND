"""Ninguna operación de la API queda abierta por descuido: sin sesión responde 401 o 403."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from app.db.session import get_session, get_session_factory
from app.main import app
from app.settings import get_settings

# Operaciones públicas a propósito; cualquier otra exige sesión.
PUBLIC_OPERATIONS = {
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/session"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/access"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/seguimiento"),
    ("POST", "/api/v1/solicitudes"),
    ("POST", "/api/v1/solicitudes/direccion"),
    ("GET", "/api/v1/inteligencia/agrupaciones"),
    ("POST", "/api/v1/inteligencia/rutas/{}/explicar"),
    ("POST", "/api/v1/cliente/chat"),
    ("GET", "/api/v1/cliente/chat/estado"),
}


@pytest_asyncio.fixture
async def anonymous_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", "matrix-session-secret-with-more-than-32-bytes")
    monkeypatch.setenv("NODE_ENV", "test")
    get_settings.cache_clear()

    async def no_session() -> AsyncIterator[None]:
        yield None  # sin cookie la autenticación nunca consulta la base de datos

    app.dependency_overrides[get_session] = no_session
    app.dependency_overrides[get_session_factory] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_session_factory, None)
        get_settings.cache_clear()


def _sample(parameter: dict[str, object]) -> str:
    schema = parameter.get("schema", {})
    assert isinstance(schema, dict)
    kind = schema.get("type")
    if kind == "integer":
        return "1"
    if kind == "number":
        return "1.2"
    return str(uuid.uuid4())


def _operations() -> list[tuple[str, str, dict[str, str], dict[str, str]]]:
    rows = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            path_values: dict[str, str] = {}
            query_values: dict[str, str] = {}
            for parameter in operation.get("parameters", []):
                if parameter["in"] == "path":
                    path_values[parameter["name"]] = _sample(parameter)
                elif parameter["in"] == "query" and parameter.get("required"):
                    query_values[parameter["name"]] = _sample(parameter)
            rows.append((method.upper(), path, path_values, query_values))
    return rows


@pytest.mark.asyncio
async def test_every_non_public_operation_rejects_anonymous_callers(
    anonymous_client: httpx.AsyncClient,
) -> None:
    operations = _operations()
    assert len(operations) >= 45
    open_operations: list[str] = []
    for method, path, path_values, query_values in operations:
        if (method, path) in PUBLIC_OPERATIONS:
            continue
        url = path
        for name, value in path_values.items():
            url = url.replace("{" + name + "}", value)
        response = await anonymous_client.request(
            method,
            url,
            params=query_values,
            headers={"Origin": "http://localhost:3000"} if method != "GET" else {},
            json={} if method in {"POST", "PUT", "PATCH"} else None,
        )
        if response.status_code not in {401, 403}:
            open_operations.append(f"{method} {path} -> {response.status_code}")
    assert open_operations == []


@pytest.mark.asyncio
async def test_public_tracking_is_rate_limited_per_guide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import tracking
    from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter

    monkeypatch.delenv("AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(
        tracking, "_TRACKING_LIMITER", CustomerChatRateLimiter(max_requests=3, window_seconds=60)
    )

    async def no_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_session] = no_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            codes = []
            for _ in range(4):
                response = await client.get(
                    "/api/v1/seguimiento", params={"guia": "guia-de-prueba"}
                )
                codes.append(response.status_code)
            other = await client.get("/api/v1/seguimiento", params={"guia": "otra-guia"})
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()
    assert codes[-1] == 429
    assert 429 not in codes[:3]
    assert other.status_code != 429
