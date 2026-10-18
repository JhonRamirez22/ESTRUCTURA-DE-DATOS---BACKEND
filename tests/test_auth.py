from __future__ import annotations

import base64
import hashlib
import hmac

from app.core.auth import (
    cleared_session_cookie,
    create_courier_access_code,
    create_session_token,
    hash_courier_access_code,
    matches_access_code,
    read_session_token,
    session_cookie,
    verify_courier_access_code,
    verify_session_token,
)

SESSION_SECRET = "unit-test-session-secret-with-at-least-32-bytes"


def test_session_signature_preserves_the_existing_javascript_token_format() -> None:
    token = create_session_token("dispatcher", 2_000, SESSION_SECRET)
    encoded_payload, encoded_signature = token.split(".")
    payload = base64.urlsafe_b64decode(encoded_payload + "==").decode("utf-8")
    expected_signature = hmac.new(
        SESSION_SECRET.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()

    assert payload == '{"role":"dispatcher","expiresAt":2000}'
    assert encoded_signature == base64.urlsafe_b64encode(expected_signature).decode().rstrip("=")
    assert verify_session_token(token, SESSION_SECRET, now_seconds=1_999) == {
        "role": "dispatcher",
        "expiresAt": 2_000,
    }


def test_session_verification_rejects_expired_tampered_and_short_secret_tokens() -> None:
    token = create_session_token(
        "courier", 2_000, SESSION_SECRET, "f8fbc95f-643e-4fb3-9b82-8c42e0707311"
    )

    assert verify_session_token(token, SESSION_SECRET, now_seconds=2_000) is None
    assert verify_session_token(f"{token[:-1]}x", SESSION_SECRET, now_seconds=1_999) is None
    assert verify_session_token(token, "short", now_seconds=1_999) is None
    assert verify_session_token(f"{token}.extra", SESSION_SECRET, now_seconds=1_999) is None


def test_courier_access_code_hash_is_salted_and_verifiable() -> None:
    courier_id = "f8fbc95f-643e-4fb3-9b82-8c42e0707311"
    access_code = create_courier_access_code(courier_id)
    encoded_hash = hash_courier_access_code(access_code)

    assert access_code.startswith(f"{courier_id}.")
    assert len(access_code.split(".", 1)[1]) >= 40
    assert not encoded_hash.endswith(access_code)
    assert verify_courier_access_code(access_code, encoded_hash)
    assert not verify_courier_access_code(f"{access_code}x", encoded_hash)
    assert not verify_courier_access_code(access_code, "unsupported:hash:format")


def test_access_code_comparison_and_cookie_contract() -> None:
    token = "eyJ0ZXN0IjoidG9rZW4ifQ.signature"

    assert matches_access_code("same-code", "same-code")
    assert not matches_access_code("same-code", "other-code")
    assert read_session_token(f"other=x; rutas-pasto-session={token}") == token
    assert read_session_token("other=x") is None
    assert "HttpOnly" in session_cookie(token, secure=True)
    assert "SameSite=Strict" in session_cookie(token, secure=True)
    assert "Secure" in session_cookie(token, secure=True)
    assert "Max-Age=0" in cleared_session_cookie(secure=False)
