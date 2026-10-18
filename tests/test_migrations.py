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


def test_account_migration_upgrades_0007_without_changing_existing_couriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "accounts-upgrade.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            courier = Courier(
                name="Repartidor existente", phone="3000000000", status=CourierStatus.OFFLINE
            )
            session.add(courier)
            session.commit()
            courier_id = courier.id
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE accounts")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
        get_settings.cache_clear()
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        command.stamp(config, "0007_shared_chat_rate_limit")
        command.upgrade(config, "head")
        assert "accounts" in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.get(Courier, courier_id) is not None
    finally:
        get_settings.cache_clear()
        engine.dispose()


def test_initial_migration_adopts_existing_tables_without_losing_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "existing.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            courier = Courier(
                name="Repartidor de prueba",
                phone="3000000000",
                status=CourierStatus.OFFLINE,
            )
            session.add(courier)
            session.commit()
            courier_id = courier.id

