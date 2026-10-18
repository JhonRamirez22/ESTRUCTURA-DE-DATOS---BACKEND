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
                "(account_id IS NULL AND delivery_point_id IS NOT NULL)",
                name="access_sessions_single_principal_check",
            ),
        )
        op.create_index("access_sessions_account_idx", "access_sessions", ["account_id"])
        op.create_index("access_sessions_expires_idx", "access_sessions", ["expires_at"])
    if "security_audit_events" not in existing:
        op.create_table(
            "security_audit_events",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="SET NULL")
            ),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("request_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("security_audit_events_created_idx", "security_audit_events", ["created_at"])


def downgrade() -> None:
    # No se usa durante rollback de aplicación: preserva auditoría y las sesiones emitidas.
    raise RuntimeError("Rollback de código únicamente; conserva las tablas de seguridad.")
