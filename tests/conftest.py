import os

os.environ.setdefault("LEGACY_API_ALIAS_ENABLED", "true")  # antes de importar la app

from collections.abc import Iterator

import pytest

from app.api.auth import get_optional_session
from app.main import app
from tests.auth_helpers import business_identity

_BUSINESS_TESTS = {
    "test_analytics_api", "test_assignments_api", "test_couriers_api",
    "test_notifications_api", "test_orders_api", "test_routes_api",
    "test_postgres_integration", "test_order_requests_api",
}


@pytest.fixture(autouse=True)
def business_authentication(request: pytest.FixtureRequest) -> Iterator[None]:
    # Estos tests aíslan reglas de negocio; test_auth_api no usa esta sustitución.
    if request.module.__name__.rsplit(".", 1)[-1] not in _BUSINESS_TESTS:
        yield
        return
    app.dependency_overrides[get_optional_session] = business_identity
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_optional_session, None)


@pytest.fixture(autouse=True)
def customer_notifications_for_feature_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    # El producto las trae apagadas; las pruebas de la función las encienden explícitamente.
    from app.settings import get_settings

    monkeypatch.setenv("CUSTOMER_NOTIFICATIONS_ENABLED", "true")
    get_settings.cache_clear()
