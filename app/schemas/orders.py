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
from app.services.scheduling import DeliverySchedule

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
    priority: int = Field(default=1, ge=1, le=3)
    scheduled_for: datetime | None = Field(default=None, validation_alias="scheduledFor")
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

    @field_validator("scheduled_for")
    @classmethod
    def _schedule_in_range(cls, value: datetime | None) -> datetime | None:
        return DeliverySchedule().parse(value)

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
