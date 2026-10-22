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
