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
    session_cookie_name,
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


