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

        _upgrade_to_head(database_path, monkeypatch)

        with Session(engine) as session:
            preserved = session.get(Courier, courier_id)
            assert preserved is not None
            assert preserved.name == "Repartidor de prueba"
            assert preserved.phone == "3000000000"
    finally:
        engine.dispose()


def test_initial_migration_rejects_existing_schema_missing_required_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "missing-index.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP INDEX "routes_courier_id_idx"')
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="routes.index:routes_courier_id_idx"):
        _upgrade_to_head(database_path, monkeypatch)


def test_customer_notification_migration_upgrades_an_adopted_old_schema_without_data_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "adopted.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE customer_notification_outbox")
            connection.exec_driver_sql(
                "ALTER TABLE delivery_points DROP COLUMN email_notifications_enabled"
            )
            connection.exec_driver_sql("ALTER TABLE delivery_points DROP COLUMN customer_email")
    finally:
        engine.dispose()

    _stamp_baseline_and_upgrade(database_path, monkeypatch)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        delivery_columns = {column["name"] for column in inspector.get_columns("delivery_points")}
        assert {"customer_email", "email_notifications_enabled"} <= delivery_columns
        assert "customer_notification_outbox" in inspector.get_table_names()
        assert {
            "customer_notification_outbox_status_available_idx",
            "customer_notification_outbox_delivery_point_idx",
        } <= {index["name"] for index in inspector.get_indexes("customer_notification_outbox")}
        assert (
            "customer_notification_outbox_event_key_key",
            ("event_key",),
        ) in actual_unique_signatures(inspector, "customer_notification_outbox")
    finally:
        engine.dispose()


def test_sms_migration_preserves_existing_email_outbox_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "sms-outbox-upgrade.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE customer_notification_outbox")
            connection.exec_driver_sql(
                """
                CREATE TABLE customer_notification_outbox (
                    id TEXT PRIMARY KEY,
                    delivery_point_id TEXT NOT NULL,
                    event_key VARCHAR(220) NOT NULL UNIQUE,
                    event_type VARCHAR(20) NOT NULL,
                    recipient_email VARCHAR(320) NOT NULL,
                    subject VARCHAR(180) NOT NULL,
                    text_body TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at DATETIME NOT NULL,
                    lease_expires_at DATETIME,
                    created_at DATETIME NOT NULL,
                    sent_at DATETIME,
                    last_error_code VARCHAR(100)
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO customer_notification_outbox (
                    id, delivery_point_id, event_key, event_type, recipient_email,
                    subject, text_body, status, attempt_count, available_at, created_at
                ) VALUES (
                    'legacy-id', 'legacy-point', 'order_assigned:route:point',
                    'ORDER_ASSIGNED', 'cliente@example.com', 'Asignado', 'Aviso',
                    'PENDING', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
    finally:
        engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    try:
        command.stamp(config, "0002_customer_email_outbox")
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
