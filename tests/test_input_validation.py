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
