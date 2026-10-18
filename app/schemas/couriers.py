"""Contratos camelCase de repartidores y eventos GPS para el frontend TypeScript."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models.enums import CourierStatus
from app.schemas.auth import AccountCredentials


class CourierCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    account: AccountCredentials | None = None


class CourierUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1)
    phone: str | None = Field(default=None, min_length=1)
    status: CourierStatus | None = None


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


class CourierCreatedResponse(BaseModel):
    courier: CourierView
    access_code: str | None = Field(default=None, serialization_alias="accessCode")


class CourierLocationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lat: float = Field(strict=True, ge=-90, le=90)
    lng: float = Field(strict=True, ge=-180, le=180)
    recorded_at: str | None = Field(default=None, validation_alias="recordedAt")


class LocationEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    courier_id: UUID = Field(serialization_alias="courierId")
    lat: float
    lng: float
    recorded_at: datetime = Field(serialization_alias="recordedAt")

    @field_serializer("recorded_at")
    def serialize_recorded_at(self, value: datetime) -> str:
        result = _serialize_utc(value)
        if result is None:
            raise ValueError("recordedAt no puede ser nulo")
        return result


class LocationEventResponse(BaseModel):
    location_event: LocationEventView = Field(serialization_alias="locationEvent")
    accepted: bool


class CourierSelfView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: CourierStatus
    current_lat: float | None = Field(serialization_alias="currentLat")
    current_lng: float | None = Field(serialization_alias="currentLng")
    last_location_at: datetime | None = Field(serialization_alias="lastLocationAt")

    @field_serializer("last_location_at")
    def serialize_last_location(self, value: datetime | None) -> str | None:
        return _serialize_utc(value)


class CourierSelfResponse(BaseModel):
    courier: CourierSelfView


class CourierStoppedResponse(BaseModel):
    courier: CourierSelfView


class ErrorResponse(BaseModel):
    error: str


def _serialize_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
