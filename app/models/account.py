"""Cuentas privadas; el rol se asigna desde despacho, nunca desde el login."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("email", name="accounts_email_key"),
        UniqueConstraint("courier_id", name="accounts_courier_id_key"),
        CheckConstraint(
            "(role = 'dispatcher' AND courier_id IS NULL) OR "
            "(role = 'courier' AND courier_id IS NOT NULL)",
            name="accounts_role_courier_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    courier_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("couriers.id", ondelete="CASCADE")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
