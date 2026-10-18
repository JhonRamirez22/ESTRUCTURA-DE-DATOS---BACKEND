"""Contratos HTTP de autenticación."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AccountCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("Ingresa un correo válido.")
        return normalized

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


class CourierAccountRequest(AccountCredentials):
    courier_id: str = Field(alias="courierId")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    access_code: str = Field(alias="accessCode", min_length=1, max_length=512)
    return_to: str | None = Field(default=None, alias="returnTo")


class LoginResponse(BaseModel):
    authenticated: bool
    redirect_to: str = Field(serialization_alias="redirectTo")


class SessionResponse(BaseModel):
    authenticated: bool


class PageAccessResponse(BaseModel):
    allowed: bool
    redirect_to: str | None = Field(default=None, serialization_alias="redirectTo")


class LogoutResponse(BaseModel):
    ok: bool
