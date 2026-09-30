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

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE")
    )
    delivery_point_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("delivery_points.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"
    __table_args__ = (Index("security_audit_events_created_idx", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
