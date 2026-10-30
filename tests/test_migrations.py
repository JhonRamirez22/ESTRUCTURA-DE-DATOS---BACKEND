from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, create_engine, inspect
from sqlalchemy.orm import Session

import app.models  # noqa: F401 — registra todas las tablas en metadata.
from alembic import command
from app.db.base import Base
from app.db.schema_validation import actual_unique_signatures
from app.models import Courier, CourierStatus, DeliveryPoint
from app.settings import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_revision_ids_fit_the_version_column() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    overlong_revisions = [
        revision.revision
        for revision in ScriptDirectory.from_config(config).walk_revisions()
        if len(revision.revision) > 32
    ]

    assert overlong_revisions == []


def _upgrade_to_head(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    try:
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()


def _stamp_baseline_and_upgrade(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    try:
        command.stamp(config, "0001_adopt_prisma_schema")
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()


def test_initial_migration_creates_all_tables_on_an_empty_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fresh.sqlite3"

    _upgrade_to_head(database_path, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        actual_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert set(Base.metadata.tables) <= actual_tables
    assert "alembic_version" in actual_tables


