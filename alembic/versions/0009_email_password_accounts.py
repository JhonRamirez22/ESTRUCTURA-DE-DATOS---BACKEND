"""Agrega cuentas sin modificar repartidores ni credenciales históricas."""

import sqlalchemy as sa

from alembic import op
from app.db.schema_validation import actual_unique_signatures

revision = "0009_email_password_accounts"
down_revision = "0008_route_optimized_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "accounts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("accounts")}
        if columns != {"id", "email", "password_hash", "role", "courier_id", "enabled"}:
            raise RuntimeError("La tabla accounts existente no coincide con el esquema esperado.")
        required_unique = {
            ("accounts_email_key", ("email",)),
            ("accounts_courier_id_key", ("courier_id",)),
        }
        if not required_unique <= actual_unique_signatures(inspector, "accounts"):
            raise RuntimeError("La tabla accounts no conserva la unicidad de correo y repartidor.")
        return
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "courier_id", sa.Uuid(), sa.ForeignKey("couriers.id", ondelete="CASCADE")
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("email", name="accounts_email_key"),
        sa.UniqueConstraint("courier_id", name="accounts_courier_id_key"),
        sa.CheckConstraint(
            "(role = 'dispatcher' AND courier_id IS NULL) OR "
            "(role = 'courier' AND courier_id IS NOT NULL)",
            name="accounts_role_courier_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("accounts")
