from __future__ import annotations

import pytest
from pydantic import SecretStr

from scripts.bootstrap_database_role import _required_secret, _validate_identifier


def test_database_bootstrap_requires_non_empty_secrets() -> None:
    assert _required_secret(SecretStr("  app-password  "), "DATABASE_PASSWORD") == ("app-password")

    with pytest.raises(ValueError, match="DATABASE_PASSWORD"):
        _required_secret(SecretStr("  "), "DATABASE_PASSWORD")


@pytest.mark.parametrize("identifier", ["rutas_app", "_worker", "A1"])
def test_database_bootstrap_accepts_safe_postgres_identifiers(identifier: str) -> None:
    _validate_identifier(identifier, "DATABASE_USERNAME")


@pytest.mark.parametrize("identifier", ["rutas-app", "1rutas", 'admin"; DROP ROLE admin'])
def test_database_bootstrap_rejects_unsafe_postgres_identifiers(identifier: str) -> None:
    with pytest.raises(ValueError, match="DATABASE_USERNAME"):
        _validate_identifier(identifier, "DATABASE_USERNAME")
