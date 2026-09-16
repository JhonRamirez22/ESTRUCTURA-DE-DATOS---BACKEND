"""Sesiones revocables y auditoría sin credenciales ni datos de pedidos."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccessSession(Base):
    __tablename__ = "access_sessions"
    __table_args__ = (
        CheckConstraint(
            "(account_id IS NOT NULL AND delivery_point_id IS NULL) OR "
            "(account_id IS NULL AND delivery_point_id IS NOT NULL)",
            name="access_sessions_single_principal_check",
        ),
        Index("access_sessions_account_idx", "account_id"),
        Index("access_sessions_expires_idx", "expires_at"),
    )

