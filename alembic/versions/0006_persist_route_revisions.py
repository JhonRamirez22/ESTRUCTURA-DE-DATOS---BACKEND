"""Persiste snapshots LIFO para deshacer recálculos de rutas."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    Uuid,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

_JSON_COLUMN_TYPE = JSON().with_variant(JSONB(), "postgresql")

revision = "0006_persist_route_revisions"
down_revision = "0005_customer_notify_deadletter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_name = "route_revisions"
    if table_name in inspector.get_table_names():
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {"id", "route_id", "revision_number", "snapshot", "created_at"}
