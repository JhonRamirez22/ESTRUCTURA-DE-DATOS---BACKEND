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
