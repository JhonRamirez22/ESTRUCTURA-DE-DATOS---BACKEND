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
