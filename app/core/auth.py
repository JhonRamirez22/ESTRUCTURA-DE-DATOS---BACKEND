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

LEGACY_SESSION_COOKIE_NAME = "rutas-pasto-session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SCRYPT_KEY_LENGTH = 64
SCRYPT_N = 16_384
SCRYPT_R = 8
SCRYPT_P = 1

SessionRole = Literal["dispatcher", "courier"]
SESSION_COOKIE_NAMES: dict[SessionRole, str] = {
    "dispatcher": "rutas-pasto-dispatch-session",
    "courier": "rutas-pasto-courier-session",
}


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
