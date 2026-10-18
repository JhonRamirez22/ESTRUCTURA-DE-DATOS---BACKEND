"""Identidad simulada solo para pruebas de negocio; auth_api prueba las sesiones reales."""

from fastapi import Request

from app.api.auth import _configured_secret
from app.core.auth import SessionClaims, read_session_token, verify_session_token
from app.settings import get_settings


async def business_identity(request: Request) -> SessionClaims | None:
    return verify_session_token(
        read_session_token(request.headers.get("cookie")), _configured_secret(get_settings())
    )
