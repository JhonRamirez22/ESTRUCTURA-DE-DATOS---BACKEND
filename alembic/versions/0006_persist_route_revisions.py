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
        missing_columns = expected_columns - existing_columns
        if missing_columns:
            raise RuntimeError(
                "route_revisions no coincide con el schema esperado: "
                + ", ".join(sorted(missing_columns))
            )
        return

    op.create_table(
        table_name,
        Column("id", Uuid(as_uuid=True), primary_key=True, nullable=False),
        Column(
            "route_id",
            Uuid(as_uuid=True),
            ForeignKey("routes.id", ondelete="CASCADE", onupdate="CASCADE"),
            nullable=False,
        ),
        Column("revision_number", Integer, nullable=False),
        Column("snapshot", _JSON_COLUMN_TYPE, nullable=False),
        Column(
            "created_at",
            DateTime(timezone=False),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        UniqueConstraint(
            "route_id", "revision_number", name="route_revisions_route_number_key"
        ),
    )


def downgrade() -> None:
    # Los snapshots forman parte del historial operativo; no se borran al revertir código.
    pass
