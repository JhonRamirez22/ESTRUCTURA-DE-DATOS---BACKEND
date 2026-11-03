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
