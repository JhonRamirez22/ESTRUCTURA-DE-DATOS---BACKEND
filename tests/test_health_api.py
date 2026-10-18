from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_session
from app.main import app


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
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}
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
        "checks": {"database": "unavailable"},
    }
    assert "private connection detail" not in response.text
