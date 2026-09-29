"""Login y sesiones compatibles con los contratos existentes de la PWA."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC
from typing import Annotated, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.core.auth import (
    LEGACY_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAMES,
    SessionClaims,
    SessionRole,
    cleared_session_cookie,
    matches_access_code,
    read_bearer_token,
    read_session_token,
    session_cookie,
    session_cookie_name,
    verify_courier_access_code,
)
from app.core.passwords import hash_password, verify_password
from app.db.session import get_session
from app.models import Account, Courier, CustomerChatRateLimitWindow, SecurityAuditEvent
from app.repositories.courier_repository import CourierRepository
from app.schemas.auth import (
    CourierAccountRequest,
    EmailLoginRequest,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    PageAccessResponse,
    PasswordChangeRequest,
    SessionResponse,
)
from app.services.access_sessions import (
    SessionIdentity,
    create_access_session,
    csrf_matches,
    csrf_token,
    resolve_access_session,
    revoke_access_session,
    revoke_account_sessions,
    utc_now,
)
from app.services.client_identity import client_key
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.request_limiter import SlidingWindowLimiter
from app.settings import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_ROLE_HEADER = "X-Session-Role"
DEVELOPMENT_DISPATCHER_CODE = "rutas-pasto-local-dispatcher-code"
DEVELOPMENT_SESSION_SECRET = "rutas-pasto-local-session-secret-not-for-production"
_DUMMY_PASSWORD_HASH = hash_password("dummy-account-password-never-used")
LOGIN_ATTEMPT_LIMIT = 10
LOGIN_WINDOW_SECONDS = 60
# Límite estricto por correo y cliente: el bloqueo de un atacante no alcanza a los demás.
_LOGIN_LIMITER = CustomerChatRateLimiter(
    max_requests=LOGIN_ATTEMPT_LIMIT, window_seconds=LOGIN_WINDOW_SECONDS
)
# Cambiar la contraseña exige la actual: se limita para que una sesión robada no pueda adivinarla.
PASSWORD_CHANGE_ATTEMPT_LIMIT = 5
_PASSWORD_LIMITER = CustomerChatRateLimiter(
    max_requests=PASSWORD_CHANGE_ATTEMPT_LIMIT, window_seconds=600
)
# Tope amplio por correo: frena la fuerza bruta repartida entre muchos clientes sin que un solo
# atacante pueda alcanzarlo (su propio límite lo detiene antes: 10 por minuto).
LOGIN_EMAIL_ATTEMPT_LIMIT = 120
LOGIN_EMAIL_WINDOW_SECONDS = 600
_LOGIN_EMAIL_LIMITER = CustomerChatRateLimiter(
    max_requests=LOGIN_EMAIL_ATTEMPT_LIMIT, window_seconds=LOGIN_EMAIL_WINDOW_SECONDS
)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@router.post("/login", response_model=LoginResponse, response_model_by_alias=True)
async def login(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    _validate_origin(request, required=True)
    try:
        body: object = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("El body debe ser JSON válido.", 400)
    if not isinstance(body, dict):
        return _error("El body debe ser un objeto JSON.", 400)

    if "email" in body or "password" in body:
        return await _login_with_password(body, session, request)
    if not get_settings().auth_legacy_code_login_enabled:
        return _error("Ingresa tu correo y contraseña.", 400)

    access_code = body.get("accessCode")
    if not isinstance(access_code, str) or not access_code:
        return _error("Ingresa tu código privado de acceso.", 400)
    if len(access_code) > 512:
        return _error("El código de acceso no es válido.", 401)
    try:
        login_request = LoginRequest.model_validate(
            {
                "accessCode": access_code,
                "returnTo": body.get("returnTo") if isinstance(body.get("returnTo"), str) else None,
            }
        )
    except ValidationError:
        return _error("Ingresa tu código privado de acceso.", 400)
    access_code = login_request.access_code

    settings = get_settings()
    secret = _configured_secret(settings)
    if secret is None:
        return _error("El acceso no está configurado: falta AUTH_SECRET (mínimo 32 bytes).", 503)

    dispatcher_code = _dispatcher_code(settings)
    if not dispatcher_code or (
        not settings.is_development and len(dispatcher_code.encode("utf-8")) < 32
    ):
        return _error("El acceso de personal no está configurado correctamente.", 503)

    if matches_access_code(access_code, dispatcher_code):
        role: SessionRole = "dispatcher"
        courier_id: str | None = None
    else:
        courier_id = await _verify_courier_login(access_code, session)
        if courier_id is None:
            return _error("Código de acceso inválido.", 401)
        role = "courier"

    account = await session.scalar(
        select(Account).where(
            Account.courier_id == UUID(courier_id) if courier_id else
            Account.email == "legacy-dispatcher@local.invalid"
        )
    )
    if account is None:
        # Solo para la transición explícita de códigos; nunca crea una contraseña conocida.
        import secrets

