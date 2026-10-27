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
