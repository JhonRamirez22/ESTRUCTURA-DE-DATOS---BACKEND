from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_session
from app.main import app
from app.settings import get_settings

_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


@pytest.fixture(autouse=True)
def _auth_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_SECRET", _SECRET)
    get_settings.cache_clear()


class _HealthySession:
    async def execute(self, _statement: object) -> None:
        return None


class _UnavailableSession:
    async def execute(self, _statement: object) -> None:
        raise SQLAlchemyError("private connection detail must not be returned")


def _dependency(session: object):
    async def override() -> AsyncIterator[object]:
        yield session

    return override


@pytest.mark.asyncio
async def test_health_returns_database_status_without_caching() -> None:
    app.dependency_overrides[get_session] = _dependency(_HealthySession())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "auth": "ok"}}
    assert response.headers["cache-control"] == "no-store, max-age=0"


@pytest.mark.asyncio
async def test_health_hides_database_exception_details() -> None:
    app.dependency_overrides[get_session] = _dependency(_UnavailableSession())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "auth": "ok"},
    }
    assert "private connection detail" not in response.text


@pytest.mark.asyncio
async def test_health_fails_when_the_session_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    app.dependency_overrides[get_session] = _dependency(_HealthySession())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()

    assert response.status_code == 503
    assert response.json()["checks"]["auth"] == "missing"
