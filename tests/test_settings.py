from __future__ import annotations

import ssl
from unittest.mock import patch

import pytest

from app.settings import Settings


def test_empty_map_coordinate_environment_values_use_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_MAP_DEFAULT_LAT", "")
    monkeypatch.setenv("NEXT_PUBLIC_MAP_DEFAULT_LNG", "")
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    settings = Settings()

    assert settings.next_public_map_default_lat == 1.2136
    assert settings.next_public_map_default_lng == -77.2811


def test_split_database_settings_build_an_encoded_verified_rds_url() -> None:
    settings = Settings(
        database_url=None,
        database_host="rutas-db.example.us-east-1.rds.amazonaws.com",
        database_port=5432,
        database_name="rutas_pasto",
        database_username="rutas_app",
        database_password="password/with@symbols",
        database_ssl_mode="verify-full",
        database_ssl_root_cert="/etc/ssl/certs/rds-global-bundle.pem",
    )

    url = settings.sqlalchemy_database_url

    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "password/with@symbols"
    assert url.query == {}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with patch("app.settings.ssl.create_default_context", return_value=context) as create:
        assert settings.sqlalchemy_connect_args == {"ssl": context}
    create.assert_called_once_with(cafile="/etc/ssl/certs/rds-global-bundle.pem")
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert "password/with@symbols" not in url.render_as_string(hide_password=True)


def test_verified_rds_connection_requires_the_aws_ca_bundle() -> None:
    settings = Settings(
        database_url=None,
        database_host="rutas-db.example.us-east-1.rds.amazonaws.com",
        database_name="rutas_pasto",
        database_username="rutas_app",
        database_password="test-only-password",
        database_ssl_mode="verify-full",
    )

    with pytest.raises(ValueError, match="DATABASE_SSL_ROOT_CERT"):
        _ = settings.sqlalchemy_connect_args
