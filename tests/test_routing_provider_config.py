from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import app.services.route_planner as route_planner
from app.core.routing.ors_client import OrsClient
from app.core.routing.osrm_client import OsrmClient
from app.services.route_planner import close_routing_clients, get_routing_providers
from app.settings import get_settings


@pytest_asyncio.fixture
async def clean_routing_singleton() -> AsyncIterator[None]:
    await close_routing_clients()
    get_settings.cache_clear()
    yield
    await close_routing_clients()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_osrm_is_primary_even_when_an_ors_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    clean_routing_singleton: None,
) -> None:
    monkeypatch.setenv("ORS_API_KEY", "configured-but-not-selected")
    monkeypatch.delenv("ROUTING_PROVIDER", raising=False)
    get_settings.cache_clear()

    matrix_provider, route_provider = get_routing_providers()

    assert route_planner._routing_clients is not None
