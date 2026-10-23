"""Comparte el límite del chat entre procesos FastAPI."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, inspect

from alembic import op

revision = "0007_shared_chat_rate_limit"
down_revision = "0006_persist_route_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table_name = "customer_chat_rate_limit_windows"
    inspector = inspect(op.get_bind())
    if table_name in inspector.get_table_names():
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {"guide_hash", "window_started_at", "request_count"}
        missing_columns = expected_columns - existing_columns
        if missing_columns:
            raise RuntimeError(
                "customer_chat_rate_limit_windows no coincide con el schema esperado: "
                + ", ".join(sorted(missing_columns))
            )
        return

    op.create_table(
        table_name,
        Column("guide_hash", String(64), primary_key=True, nullable=False),
        Column("window_started_at", DateTime(timezone=False), nullable=False),
        Column("request_count", Integer, nullable=False),
    )
    op.create_index(
        "customer_chat_rate_limit_windows_started_at_idx",
        table_name,
        ["window_started_at"],
    )


def downgrade() -> None:
    table_name = "customer_chat_rate_limit_windows"
    if table_name in inspect(op.get_bind()).get_table_names():
        op.drop_index(
            "customer_chat_rate_limit_windows_started_at_idx",
            table_name=table_name,
        )
        op.drop_table(table_name)
