"""Contratos camelCase de repartidores y eventos GPS para el frontend TypeScript."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_serializer, field_validator

from app.core.validation import plain_text
from app.models.enums import CourierStatus
from app.schemas.auth import AccountCredentials


class CourierCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=20)
    account: AccountCredentials | None = None

    @field_validator("name", "phone")
    @classmethod
    def _plain(cls, value: str, info: ValidationInfo) -> str:
        return plain_text(value, max_length=100, field=str(info.field_name))


class CourierUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, min_length=1, max_length=20)
    status: CourierStatus | None = None

    @field_validator("name", "phone")
    @classmethod
    def _plain(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return plain_text(value, max_length=100, field=str(info.field_name))


class CourierView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    phone: str
    status: CourierStatus
    current_lat: float | None = Field(serialization_alias="currentLat")
    current_lng: float | None = Field(serialization_alias="currentLng")
    last_location_at: datetime | None = Field(serialization_alias="lastLocationAt")

    @field_serializer("last_location_at")
    def serialize_last_location(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class CourierCollectionResponse(BaseModel):
    couriers: list[CourierView]


class CourierResponse(BaseModel):
    courier: CourierView


