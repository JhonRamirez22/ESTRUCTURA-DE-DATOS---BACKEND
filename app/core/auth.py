"""Firmas de sesión y credenciales de repartidor compatibles con el backend previo."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Literal, NotRequired, TypedDict, cast

SESSION_COOKIE_NAME = "rutas-pasto-session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SCRYPT_KEY_LENGTH = 64
SCRYPT_N = 16_384
SCRYPT_R = 8
SCRYPT_P = 1

SessionRole = Literal["dispatcher", "courier"]


class SessionClaims(TypedDict):
    role: SessionRole
    expiresAt: int
    courierId: NotRequired[str]
    accountId: NotRequired[str]


def create_courier_access_code(courier_id: str) -> str:
    """Crea un código de un solo repartidor sin persistir el secreto en claro."""
    return f"{courier_id}.{secrets.token_urlsafe(32)}"


def hash_courier_access_code(access_code: str) -> str:
    """Usa los parámetros por defecto de Node scryptSync para conservar hashes existentes."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        access_code.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_LENGTH,
    )
    return f"scrypt:{_encode_base64url(salt)}:{_encode_base64url(digest)}"


def verify_courier_access_code(access_code: str, encoded_hash: str) -> bool:
    parts = encoded_hash.split(":")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False

    try:
        salt = _decode_base64url(parts[1])
        expected_digest = _decode_base64url(parts[2])
        if len(salt) != 16 or len(expected_digest) != SCRYPT_KEY_LENGTH:
            return False
        actual_digest = hashlib.scrypt(
            access_code.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_KEY_LENGTH,
        )
        return hmac.compare_digest(actual_digest, expected_digest)
    except (ValueError, TypeError):
        return False


def matches_access_code(input_code: str, expected_code: str) -> bool:
    return hmac.compare_digest(input_code.encode("utf-8"), expected_code.encode("utf-8"))


def create_session_token(
    role: SessionRole,
    expires_at: int,
    secret: str,
    courier_id: str | None = None,
) -> str:
    """Firma JSON compacto con HMAC-SHA256 en el mismo formato que Next.js."""
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("AUTH_SECRET debe contener al menos 32 bytes")
    claims: dict[str, str | int] = {"role": role}
    if courier_id is not None:
        claims["courierId"] = courier_id
    claims["expiresAt"] = expires_at
    payload = _encode_base64url(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    return f"{payload}.{_encode_base64url(signature.digest())}"


def verify_session_token(
    token: str | None,
    secret: str | None,
    now_seconds: int | None = None,
) -> SessionClaims | None:
    if not token or not secret or len(secret.encode("utf-8")) < 32:
        return None

    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        return None

    payload, encoded_signature = parts
    try:
        signature = _decode_base64url(encoded_signature)
        expected_signature = hmac.new(
            secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            return None

        value: object = json.loads(_decode_base64url(payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(value, dict):
        return None
    role = value.get("role")
    expires_at = value.get("expiresAt")
    if role not in {"dispatcher", "courier"} or type(expires_at) is not int:
        return None
    current_time = int(time.time()) if now_seconds is None else now_seconds
    if expires_at <= current_time:
        return None
    courier_id = value.get("courierId")
    if role == "courier" and (not isinstance(courier_id, str) or not courier_id):
        return None

    return cast(SessionClaims, value)


def read_session_token(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    for entry in cookie_header.split(";"):
        name, separator, value = entry.strip().partition("=")
        if name == SESSION_COOKIE_NAME:
            return value if separator and value else None
    return None


def session_cookie(token: str, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_MAX_AGE_SECONDS}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def cleared_session_cookie(secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64url(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))
