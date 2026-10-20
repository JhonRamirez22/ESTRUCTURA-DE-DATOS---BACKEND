"""Token Bearer: formato estricto y rechazo de credenciales mal formadas."""

from __future__ import annotations

import pytest
from test_api_security_matrix import anonymous_client  # noqa: F401

from app.core.auth import read_bearer_token


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("Basic abc123", None),
        ("Bearer", None),
        ("Bearer a b", None),
        ("Bearer " + "x" * 600, None),
        ("", None),
        (None, None),
    ],
)
def test_read_bearer_token_format(header: str | None, expected: str | None) -> None:
    assert read_bearer_token(header) == expected


@pytest.mark.asyncio
async def test_malformed_bearer_is_unauthorized(anonymous_client) -> None:  # noqa: F811
    response = await anonymous_client.get(
        "/api/v1/pedidos", headers={"Authorization": "Bearer corto"}
    )
    assert response.status_code in {401, 403}
