from collections.abc import Iterator

import pytest

from app.api.auth import get_optional_session
from app.main import app
from tests.auth_helpers import business_identity

_BUSINESS_TESTS = {
    "test_analytics_api", "test_assignments_api", "test_couriers_api",
    "test_notifications_api", "test_orders_api", "test_routes_api",
    "test_postgres_integration",
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
