"""Respuestas públicas del buzón de notificaciones."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import NotificationKind


class NotificationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: NotificationKind
    recipient_id: str = Field(validation_alias="recipient_id", serialization_alias="recipientId")
    title: str
    message: str
    created_at: datetime = Field(validation_alias="created_at", serialization_alias="createdAt")
    read: bool


class NotificationListResponse(BaseModel):
    notifications: list[NotificationView]
    warning: str | None = None


class CustomerNotificationChannelStatus(BaseModel):
    configured: bool
    pending: int
    retrying: int
    processing: int
    failed: int


class CustomerNotificationOperationsStatus(BaseModel):
    channels: dict[Literal["email", "sms"], CustomerNotificationChannelStatus]
    worker_running: bool = Field(serialization_alias="workerRunning")
    warning: str | None = None
