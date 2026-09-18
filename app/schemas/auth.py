"""Contratos HTTP de autenticación."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.validation import normalize_email


class AccountCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def reject_common_password(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().casefold() in {
            "123456789012345",
            "passwordpassword",
            "contraseñacontraseña",
        }:
            raise ValueError("Elige una contraseña menos común.")
        return value


class EmailLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)
    return_to: str | None = Field(default=None, alias="returnTo")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return AccountCredentials.normalize_email(value)


