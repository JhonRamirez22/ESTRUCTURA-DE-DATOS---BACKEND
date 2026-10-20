from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.validation import normalize_email, plain_text
from app.schemas.auth import AccountCredentials, EmailLoginRequest
from app.schemas.couriers import CourierCreateRequest
from app.schemas.orders import OrderCreateRequest


@pytest.mark.parametrize(
    "email",
    ["a.b@correo.com", "  Usuario+tag@Dominio.CO  ", "x_y%z@sub.dominio.edu.co"],
)
def test_valid_emails_are_normalized(email: str) -> None:
    assert normalize_email(email) == email.strip().casefold()


@pytest.mark.parametrize(
    "email",
    [
        "sin-arroba.com", "@correo.com", "usuario@", "usuario@correo", "usuario@.com",
        "usuario@correo..com", "usuario@correo.c", "usu ario@correo.com", "usuario@-correo.com",
        "usuario@correo-.com", ".usuario@correo.com", "usuario.@correo.com", "usu..ario@correo.com",
        "usuário@correo.com", "a@b@correo.com", "usuario@cor\nreo.com", "<x>@correo.com",
        "a" * 65 + "@correo.com", "a@" + "b" * 250 + ".com",
    ],
)
def test_malformed_emails_are_rejected(email: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(email)


def test_login_and_account_schemas_use_the_strict_email_rule() -> None:
    with pytest.raises(ValidationError):
        EmailLoginRequest.model_validate({"email": "sin-dominio@x", "password": "una-clave-larga"})
    with pytest.raises(ValidationError):
        AccountCredentials.model_validate({"email": "a@b.c", "password": "x" * 15})
    ok = EmailLoginRequest.model_validate({"email": "Equipo@Correo.com", "password": "una-clave"})
    assert ok.email == "equipo@correo.com"


def test_plain_text_limits_length_and_control_characters() -> None:
    assert plain_text("  Calle 18 # 22-14 ", max_length=50, field="address") == "Calle 18 # 22-14"
    with pytest.raises(ValueError):
        plain_text("a" * 51, max_length=50, field="address")
    with pytest.raises(ValueError):
        plain_text("Calle\x00 18", max_length=50, field="address")
    with pytest.raises(ValueError):
        plain_text("línea1\nlínea2", max_length=50, field="address")


def test_order_schema_bounds_every_free_text_field() -> None:
    base = {"address": "Calle 18 # 22-14", "lat": 1.21, "lng": -77.28, "orderId": "PED-1"}
    assert OrderCreateRequest.model_validate(base).address == "Calle 18 # 22-14"
    for override in (
        {"address": "x" * 201},
        {"orderId": "x" * 61},
        {"address": "Calle\x00"},
        {"timeWindow": {"k": "v" * 600}},
    ):
        with pytest.raises(ValidationError):
            OrderCreateRequest.model_validate({**base, **override})


def test_courier_schema_bounds_name_phone_and_account_email() -> None:
    base = {"name": "Carlos", "phone": "3001234567"}
    assert CourierCreateRequest.model_validate(base).name == "Carlos"
    for override in ({"name": "x" * 101}, {"phone": "3" * 21}, {"name": "Car\nlos"}):
        with pytest.raises(ValidationError):
            CourierCreateRequest.model_validate({**base, **override})
    with pytest.raises(ValidationError):
        CourierCreateRequest.model_validate(
            {**base, "account": {"email": "malo@dominio", "password": "x" * 16}}
        )
