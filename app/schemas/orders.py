"""Contratos HTTP de puntos de entrega, con nombres camelCase para Next.js."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from app.core.validation import normalize_email, plain_text
from app.models.enums import DeliveryPointStatus, RouteGeometryProvider, RouteStatus

_Latitude = Annotated[float, Field(strict=True, ge=-90, le=90, allow_inf_nan=False)]
_Longitude = Annotated[float, Field(strict=True, ge=-180, le=180, allow_inf_nan=False)]
MAX_ADDRESS_LENGTH = 200
MAX_ORDER_ID_LENGTH = 60
MAX_TIME_WINDOW_JSON = 500
_COLOMBIAN_MOBILE_PATTERN = re.compile(r"^(?:\+?57)?3\d{9}$")


def _normalize_colombian_mobile(phone: str) -> str:
    compact = re.sub(r"[\s().-]", "", phone)
    if not _COLOMBIAN_MOBILE_PATTERN.fullmatch(compact):
        raise ValueError("customerPhone debe ser un celular colombiano válido.")
    digits = compact.lstrip("+")
    national_number = digits[2:] if digits.startswith("57") else digits
    return f"+57{national_number}"



def _check_text(value: str | None, field: str, max_length: int) -> str | None:
    return None if value is None else plain_text(value, max_length=max_length, field=field)


def _check_time_window(value: JsonValue | None) -> JsonValue | None:
    if value is not None and len(json.dumps(value)) > MAX_TIME_WINDOW_JSON:
        raise ValueError("timeWindow es demasiado grande.")
    return value


class OrderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    address: str = Field(min_length=1, max_length=MAX_ADDRESS_LENGTH)
    lat: _Latitude
    lng: _Longitude
    order_id: str = Field(
        min_length=1, max_length=MAX_ORDER_ID_LENGTH, validation_alias="orderId"
    )
    time_window: JsonValue | None = Field(default=None, validation_alias="timeWindow")
    customer_email: str | None = Field(
        default=None, max_length=320, validation_alias="customerEmail"
    )
    email_notifications_enabled: bool = Field(
        default=False, validation_alias="emailNotificationsEnabled"
    )
    customer_phone: str | None = Field(
        default=None, max_length=30, validation_alias="customerPhone"
    )
    sms_notifications_enabled: bool = Field(
        default=False, validation_alias="smsNotificationsEnabled"
    )

    @field_validator("address")
    @classmethod
    def _address_text(cls, value: str | None) -> str | None:
        return _check_text(value, "address", MAX_ADDRESS_LENGTH)

    @field_validator("order_id")
    @classmethod
    def _order_id_text(cls, value: str | None) -> str | None:
        return _check_text(value, "orderId", MAX_ORDER_ID_LENGTH)

    @field_validator("time_window")
    @classmethod
    def _time_window_size(cls, value: JsonValue | None) -> JsonValue | None:
        return _check_time_window(value)

    @model_validator(mode="after")
    def validate_email_consent(self) -> OrderCreateRequest:
        if self.customer_email is not None:
            self.customer_email = normalize_email(self.customer_email)
        if self.email_notifications_enabled and self.customer_email is None:
            raise ValueError("Se requiere customerEmail para activar avisos por correo.")
        if not self.email_notifications_enabled:
            # No retenemos datos de contacto que el cliente no autorizó usar.
            self.customer_email = None
        if self.sms_notifications_enabled:
            if self.customer_phone is None:
                raise ValueError("Se requiere customerPhone para activar avisos por SMS.")
            self.customer_phone = _normalize_colombian_mobile(self.customer_phone)
        else:
            # El backend no retiene un teléfono si el cliente no autorizó SMS.
            self.customer_phone = None
        return self


class OrderRequestCreate(BaseModel):
    """Solicitud pública de un cliente: solo dirección y punto; ningún dato personal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    address: str = Field(min_length=5, max_length=MAX_ADDRESS_LENGTH)
    lat: _Latitude
    lng: _Longitude

    @field_validator("address")
    @classmethod
    def _address_text(cls, value: str) -> str:
        text = plain_text(value, max_length=MAX_ADDRESS_LENGTH, field="address")
        # Entrada anónima: una dirección nunca necesita marcado.
        if "<" in text or ">" in text:
            raise ValueError("address contiene caracteres no permitidos.")
        return text


class OrderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    address: str | None = Field(default=None, min_length=1, max_length=MAX_ADDRESS_LENGTH)
    lat: _Latitude | None = None
    lng: _Longitude | None = None
    order_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_ORDER_ID_LENGTH, validation_alias="orderId"
    )
    status: DeliveryPointStatus | None = None
    time_window: JsonValue | None = Field(default=None, validation_alias="timeWindow")
    customer_email: str | None = Field(
        default=None, max_length=320, validation_alias="customerEmail"
    )
    email_notifications_enabled: bool | None = Field(
        default=None, validation_alias="emailNotificationsEnabled"
    )
    customer_phone: str | None = Field(
        default=None, max_length=30, validation_alias="customerPhone"
    )
    sms_notifications_enabled: bool | None = Field(
        default=None, validation_alias="smsNotificationsEnabled"
    )

    @field_validator("address")
    @classmethod
    def _address_text(cls, value: str | None) -> str | None:
        return _check_text(value, "address", MAX_ADDRESS_LENGTH)

    @field_validator("order_id")
    @classmethod
    def _order_id_text(cls, value: str | None) -> str | None:
        return _check_text(value, "orderId", MAX_ORDER_ID_LENGTH)

    @field_validator("time_window")
    @classmethod
    def _time_window_size(cls, value: JsonValue | None) -> JsonValue | None:
        return _check_time_window(value)

    @model_validator(mode="after")
    def validate_update(self) -> OrderUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("No hay campos válidos para actualizar.")
        nullable_fields = {"time_window", "customer_email", "customer_phone"}
        if self.customer_email is not None:
            self.customer_email = normalize_email(self.customer_email)
        if (
            "customer_email" in self.model_fields_set
            and self.customer_email is None
            and self.email_notifications_enabled is True
        ):
            raise ValueError("Se requiere customerEmail para activar avisos por correo.")
        if self.email_notifications_enabled is False:
            self.customer_email = None
        if self.sms_notifications_enabled is True:
            if self.customer_phone is None:
                raise ValueError("Se requiere customerPhone para activar avisos por SMS.")
            self.customer_phone = _normalize_colombian_mobile(self.customer_phone)
        elif self.customer_phone is not None:
            raise ValueError("Se requiere consentimiento explícito para guardar customerPhone.")
        for field_name in self.model_fields_set - nullable_fields:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} no puede ser nulo.")
        return self


class RouteSummaryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    courier_id: UUID = Field(serialization_alias="courierId")
    status: RouteStatus
    estimated_duration_minutes: float = Field(serialization_alias="estimatedDurationMinutes")
    estimated_distance_meters: float = Field(serialization_alias="estimatedDistanceMeters")
    baseline_duration_minutes: float | None = Field(
        serialization_alias="baselineDurationMinutes"
    )
    baseline_distance_meters: float | None = Field(
        serialization_alias="baselineDistanceMeters"
    )
    geometry: JsonValue | None
    geometry_provider: RouteGeometryProvider | None = Field(
        serialization_alias="geometryProvider"
    )
    started_at: datetime | None = Field(serialization_alias="startedAt")
    completed_at: datetime | None = Field(serialization_alias="completedAt")
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")

    @field_serializer("started_at", "completed_at", "created_at", "updated_at")
    def serialize_route_dates(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DeliveryPointView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    address: str
    lat: float
    lng: float
    time_window: JsonValue | None = Field(serialization_alias="timeWindow")
    status: DeliveryPointStatus
    order_id: str = Field(serialization_alias="orderId")
    tracking_token: str | None = Field(serialization_alias="trackingToken")
    route_id: UUID | None = Field(serialization_alias="routeId")
    sequence_index: int | None = Field(serialization_alias="sequenceIndex")
    route: RouteSummaryView | None = None
