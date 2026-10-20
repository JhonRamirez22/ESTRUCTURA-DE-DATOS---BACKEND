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
_LOGIN_LIMITER = CustomerChatRateLimiter(
    max_requests=LOGIN_ATTEMPT_LIMIT, window_seconds=LOGIN_WINDOW_SECONDS
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

        account = Account(
            email=f"legacy-{courier_id}@local.invalid" if courier_id else
            "legacy-dispatcher@local.invalid",
            password_hash=await run_in_threadpool(hash_password, secrets.token_urlsafe(32)),
            role=role,
            courier_id=UUID(courier_id) if courier_id else None,
        )
        session.add(account)
        await session.flush()
    if not account.enabled:
        return _error("Código de acceso inválido.", 401)
    _audit(session, request, "login", account.id)
    return await _login_response(account, session, login_request.return_to, settings, secret)


async def _login_response(
    account: Account,
    session: AsyncSession,
    return_to: str | None,
    settings: Settings,
    secret: str,
) -> JSONResponse:
    token = await create_access_session(session, account_id=account.id)
    await session.commit()
    role = cast(SessionRole, account.role)
    payload = LoginResponse(
        authenticated=True,
        redirect_to=_safe_redirect_target(role, return_to),
        access_token=token,
    )
    secure = settings.node_env == "production"
    response = JSONResponse(
        content=payload.model_dump(by_alias=True),
        headers={
            "Set-Cookie": session_cookie(token, role, secure),
            "Cache-Control": "no-store, private",
            "X-CSRF-Token": csrf_token(token, secret),
            SESSION_ROLE_HEADER: role,
        },
    )
    # La cookie compartida anterior se elimina para que no compita con las de cada rol.
    response.headers.append(
        "Set-Cookie", cleared_session_cookie(LEGACY_SESSION_COOKIE_NAME, secure)
    )
    return response


async def _login_with_password(
    body: dict[str, object], session: AsyncSession, request: Request,
) -> JSONResponse:
    try:
        credentials = EmailLoginRequest.model_validate(body)
    except ValidationError:
        return _error("Ingresa un correo válido y tu contraseña (máximo 128 caracteres).", 400)
    settings = get_settings()
    secret = _configured_secret(settings)
    if secret is None:
        return _error("El acceso no está configurado: falta AUTH_SECRET (mínimo 32 bytes).", 503)
    rate_key = f"login:{credentials.email}"
    retry_after = await _LOGIN_LIMITER.retry_after_shared(session, rate_key, hmac_secret=secret)
    if retry_after is not None:
        return JSONResponse(
            {"error": "Demasiados intentos. Espera un minuto y vuelve a ingresar."},
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
    account = await session.scalar(select(Account).where(Account.email == credentials.email))
    valid = await run_in_threadpool(
        verify_password,
        credentials.password.get_secret_value(),
        account.password_hash if account is not None else _DUMMY_PASSWORD_HASH,
    )
    if account is None or not valid or not account.enabled:
        _audit(session, request, "login", outcome="denied")
        await session.commit()
        return _error("Correo o contraseña incorrectos.", 401)
    # Descuenta solo este éxito: no borra intentos concurrentes de otras réplicas.
    digest = hmac.new(secret.encode(), rate_key.encode(), hashlib.sha256).hexdigest()
    await session.execute(
        update(CustomerChatRateLimitWindow)
        .where(CustomerChatRateLimitWindow.guide_hash == digest)
        .values(request_count=case(
            (CustomerChatRateLimitWindow.request_count > 0,
             CustomerChatRateLimitWindow.request_count - 1),
            else_=0,
        ))
    )
    _audit(session, request, "login", account.id)
    return await _login_response(
        account,
        session,
        credentials.return_to,
        settings,
        secret,
    )


@router.get("/session", response_model=SessionResponse)
async def read_session(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    settings = get_settings()
    claims = await get_optional_session(request, session)
    headers = {"Cache-Control": "no-store, private"}
    secret = _configured_secret(settings)
    if claims is not None and secret:
        role = claims["role"]
        token = read_session_token(request.headers.get("cookie"), role)
        if token:
            headers["X-CSRF-Token"] = csrf_token(token, secret)
            headers[SESSION_ROLE_HEADER] = role
    payload = SessionResponse(authenticated=claims is not None)
    return JSONResponse(
        content=payload.model_dump(),
        headers=headers,
    )


@router.get("/access", response_model=PageAccessResponse, response_model_by_alias=True)
async def check_page_access(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    path: Annotated[str, Query(min_length=1, max_length=256)],
) -> PageAccessResponse:
    if path == "/acceso":
        claims = await get_optional_session(request, session)
        if claims is None:
            return PageAccessResponse(allowed=True)
        return PageAccessResponse(allowed=False, redirect_to=_home_for_role(claims["role"]))

    required_role = _required_page_role(path)
    if required_role is None:
        raise HTTPException(status_code=400, detail="La ruta no requiere una sesión de rol.")
    # Cada rol tiene su cookie: una sesión de repartidor no impide entrar al panel de despacho.
    if await _resolve_session(request, session, role=required_role) is not None:
        return PageAccessResponse(allowed=True)
    other_role: SessionRole = "courier" if required_role == "dispatcher" else "dispatcher"
    other = await _resolve_session(request, session, role=other_role)
    if other is None:
        return PageAccessResponse(allowed=False, redirect_to="/acceso")
    return PageAccessResponse(allowed=False, redirect_to=_home_for_role(other["role"]))


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    settings = get_settings()
    secure = settings.node_env == "production"
    cookie_header = request.headers.get("cookie")
    requested = _requested_role(request)
    # Sin rol explícito se cierran todas las sesiones de este navegador.
    roles: tuple[SessionRole, ...] = (requested,) if requested else ("dispatcher", "courier")
    for role in roles:
        claims = await _resolve_session(request, session, role=role)
        await revoke_access_session(session, read_session_token(cookie_header, role))
        bearer = read_bearer_token(request.headers.get("authorization"))
        if bearer:
            await revoke_access_session(session, bearer)
        if claims and claims.get("accountId"):
            _audit(session, request, "logout", UUID(claims["accountId"]))
    await session.commit()
    response = JSONResponse(
        content={"ok": True},
        headers={"Cache-Control": "no-store, private"},
    )
    for role in roles:
        response.headers.append(
            "Set-Cookie", cleared_session_cookie(session_cookie_name(role), secure)
        )
    if requested is None:
        response.headers.append(
            "Set-Cookie", cleared_session_cookie(LEGACY_SESSION_COOKIE_NAME, secure)
        )
    return response


async def get_optional_session(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionClaims | None:
    """Dependencia de FastAPI: la sesión según `X-Session-Role` o la primera válida."""
    return await _resolve_session(request, session)


async def _resolve_session(
    request: Request,
    session: AsyncSession,
    role: SessionRole | None = None,
) -> SessionClaims | None:
    """Revalida cuenta y sesión en cada petición; las cookies firmadas antiguas no autorizan.

    Con `role` solo se consulta la cookie de ese rol. Sin él se usa el rol indicado en
    `X-Session-Role` y, si no llega, la primera sesión válida (despacho, luego repartidor).
    """
    settings = get_settings()
    secret = _configured_secret(settings)
    if secret is None:
        return None
    if role is not None:
        roles: tuple[SessionRole, ...] = (role,)
    elif (requested := _requested_role(request)) is not None:
        roles = (requested,)
    else:
        roles = ("dispatcher", "courier")
    cookie_header = request.headers.get("cookie")
    bearer = read_bearer_token(request.headers.get("authorization"))
    for candidate in roles:
        # El token Bearer tiene prioridad; la cookie HttpOnly queda como respaldo.
        token: str | None = bearer
        identity = await resolve_access_session(session, token) if token else None
        from_bearer = identity is not None and identity.role in roles
        if not from_bearer:
            token = read_session_token(cookie_header, candidate)
            identity = await resolve_access_session(session, token) if token else None
        if token is None:
            continue
        await session.commit()
        if identity is None:
            continue
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            _validate_origin(request, required=True)
            # Con Bearer no hay credencial ambiente: el CSRF solo protege a la cookie.
            if not from_bearer and not csrf_matches(
                token, request.headers.get("X-CSRF-Token"), secret
            ):
                raise HTTPException(status_code=403, detail="La verificación CSRF falló.")
        _enforce_account_limit(
            settings, identity.account_id or identity.courier_id or candidate, request.method
        )
        return _claims_from_identity(identity)
    return None


_ACCOUNT_LIMITERS: dict[tuple[bool, int], SlidingWindowLimiter] = {}


def _enforce_account_limit(settings: Settings, subject: object, method: str) -> None:
    """429 si una cuenta repite peticiones por encima de su cupo por minuto."""
    write = method not in {"GET", "HEAD", "OPTIONS"}
    limit = (
        settings.account_write_limit_per_minute
        if write
        else settings.account_read_limit_per_minute
    )
    limiter = _ACCOUNT_LIMITERS.setdefault((write, limit), SlidingWindowLimiter(limit, 60))
    retry_after = limiter.retry_after(str(subject))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes seguidas. Espera un momento.",
            headers={"Retry-After": str(retry_after), "Cache-Control": "private, no-store"},
        )


def _claims_from_identity(identity: SessionIdentity) -> SessionClaims:
    claims: SessionClaims = {
        "role": cast(SessionRole, identity.role),
        "expiresAt": int(identity.expires_at.replace(tzinfo=UTC).timestamp()),
    }
    if identity.account_id:
        claims["accountId"] = str(identity.account_id)
    if identity.courier_id:
        claims["courierId"] = str(identity.courier_id)
    return claims


def _requested_role(request: Request) -> SessionRole | None:
    value = request.headers.get(SESSION_ROLE_HEADER)
    if value is None:
        return None
    if value not in SESSION_COOKIE_NAMES:
        raise HTTPException(status_code=400, detail="El rol de sesión no es válido.")
    return value


async def _role_claims(
    role: SessionRole,
    claims: SessionClaims | None,
    request: Request,
    session: AsyncSession,
) -> SessionClaims | None:
    """Prefiere la sesión del rol pedido; si no existe, devuelve la otra para responder 403."""
    if claims is not None and claims["role"] == role:
        return claims
    own = await _resolve_session(request, session, role=role)
    return own if own is not None else claims


def _validate_origin(request: Request, *, required: bool = False) -> None:
    origin = request.headers.get("origin")
    if origin is None and not required:
        return
    allowed = get_settings().app_allowed_origins
    if get_settings().node_env != "production":
        allowed = (
            *allowed, str(request.base_url).rstrip("/"),
            "http://localhost:3000", "http://127.0.0.1:3000",
        )
    if origin not in allowed or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="El origen de la petición no está permitido.")


async def require_dispatcher_session(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> SessionClaims:
    claims = await _role_claims("dispatcher", claims, request, session)
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail="Debes iniciar sesión para continuar.",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    if claims["role"] != "dispatcher":
        raise HTTPException(
            status_code=403,
            detail="Se requiere una sesión de despacho.",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    return claims


async def require_courier_session(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> SessionClaims:
    claims = await _role_claims("courier", claims, request, session)
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail="Debes iniciar sesión para continuar.",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    if claims["role"] != "courier" or not claims.get("courierId"):
        raise HTTPException(
            status_code=403,
            detail="Se requiere una sesión de repartidor.",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    return claims


@router.put("/courier-account")
async def save_courier_account(
    request: Request,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    try:
        data = CourierAccountRequest.model_validate(await request.json())
    except (ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        return _error(
            "Ingresa un correo válido, repartidor y contraseña de 15 a 128 caracteres.", 400
        )
    try:
        courier_id = UUID(data.courier_id)
    except ValueError:
        return _error("El identificador del repartidor no es válido.", 400)
    if await session.get(Courier, courier_id) is None:
        return _error("El repartidor no existe.", 404)
    existing = await session.scalar(select(Account).where(Account.courier_id == courier_id))
    digest = await run_in_threadpool(hash_password, data.password.get_secret_value())
    if existing is None:
        session.add(
            Account(
                email=data.email,
                password_hash=digest,
                role="courier",
                courier_id=courier_id,
            )
        )
    else:
        existing.email, existing.password_hash, existing.enabled = data.email, digest, True
        await revoke_account_sessions(session, existing.id)
    _audit(session, request, "courier_account_changed", UUID(_claims["accountId"]))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _error("Ese correo ya pertenece a otra cuenta.", 409)
    return JSONResponse({"email": data.email}, headers={"Cache-Control": "no-store, private"})


async def _verify_courier_login(access_code: str, session: AsyncSession) -> str | None:
    courier_id, separator, secret_part = access_code.partition(".")
    if not separator or not _UUID_PATTERN.fullmatch(courier_id) or len(secret_part) < 40:
        return None

    courier_uuid = UUID(courier_id)
    credential = await CourierRepository(session).get_credential(courier_uuid)
    if credential is None:
        return None
    if not await run_in_threadpool(
        verify_courier_access_code, access_code, credential.access_code_hash
    ):
        return None
    return str(courier_uuid)


def _configured_secret(settings: Settings) -> str | None:
    secret = (
        settings.auth_secret.get_secret_value().strip()
        if settings.auth_secret is not None
        else None
    )
    if secret is None and settings.is_development:
        secret = DEVELOPMENT_SESSION_SECRET
    if secret is None or len(secret.encode("utf-8")) < 32:
        return None
    return secret


def _dispatcher_code(settings: Settings) -> str | None:
    access_code = (
        settings.dispatcher_access_code.get_secret_value().strip()
        if settings.dispatcher_access_code is not None
        else None
    )
    if access_code is None and settings.is_development:
        return DEVELOPMENT_DISPATCHER_CODE
    return access_code


def _required_page_role(path: str) -> SessionRole | None:
    if path == "/dashboard" or path.startswith("/dashboard/"):
        return "dispatcher"
    if path == "/pedidos" or path.startswith("/pedidos/"):
        return "dispatcher"
    if path == "/repartidor" or path.startswith("/repartidor/"):
        return "courier"
    return None


def _home_for_role(role: SessionRole) -> str:
    return "/dashboard" if role == "dispatcher" else "/repartidor"


def _safe_redirect_target(role: SessionRole, value: object) -> str:
    fallback = "/dashboard" if role == "dispatcher" else "/repartidor"
    if not isinstance(value, str) or not value.startswith("/"):
        return fallback
    if value.startswith("//") or "\\" in value:
        return fallback

    target = urlsplit(value)
    path = target.path
    if role == "dispatcher":
        allowed = path == "/dashboard" or path.startswith("/dashboard/")
        allowed = allowed or path == "/pedidos" or path.startswith("/pedidos/")
    else:
        allowed = path == "/repartidor" or path.startswith("/repartidor/")
    if not allowed or target.scheme or target.netloc:
        return fallback
    return urlunsplit(("", "", path, target.query, target.fragment))


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store, private"},
    )


def _audit(
    session: AsyncSession, request: Request, action: str,
    account_id: UUID | None = None, *, outcome: str = "allowed",
) -> None:
    session.add(SecurityAuditEvent(
        account_id=account_id, action=action, outcome=outcome,
        request_id=getattr(request.state, "request_id", str(uuid4())), created_at=utc_now(),
    ))
