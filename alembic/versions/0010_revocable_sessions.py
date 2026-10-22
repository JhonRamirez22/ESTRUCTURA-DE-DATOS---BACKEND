"""Agrega sesiones revocables y auditoría; no modifica filas de negocio."""

import sqlalchemy as sa

from alembic import op

revision = "0010_revocable_sessions"
down_revision = "0009_email_password_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # El baseline histórico puede haber creado tablas nuevas desde metadata en una BD vacía.
    existing = sa.inspect(op.get_bind()).get_table_names()
    if "access_sessions" not in existing:
        op.create_table(
            "access_sessions",
            sa.Column("token_digest", sa.String(64), primary_key=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE")),
            sa.Column(
                "delivery_point_id", sa.Uuid(),
                sa.ForeignKey("delivery_points.id", ondelete="CASCADE"),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime()),
            sa.CheckConstraint(
                "(account_id IS NOT NULL AND delivery_point_id IS NULL) OR "
