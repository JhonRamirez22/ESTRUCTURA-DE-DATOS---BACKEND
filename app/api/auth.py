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
