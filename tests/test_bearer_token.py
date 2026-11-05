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


def test_balancer_health_path_survives_without_legacy_alias() -> None:
    import os
    import subprocess
    import sys

    code = (
        "import asyncio, httpx\n"
        "from app.main import app\n"
        "async def main():\n"
        "    t = httpx.ASGITransport(app=app, raise_app_exceptions=False)\n"
        "    async with httpx.AsyncClient(transport=t, base_url='http://t') as c:\n"
        "        h = (await c.get('/api/health')).status_code\n"
        "        o = (await c.get('/api/pedidos')).status_code\n"
        "    print(h != 404, o == 404)\n"
        "asyncio.run(main())\n"
    )
    env = {**os.environ, "LEGACY_API_ALIAS_ENABLED": "false"}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out.splitlines()[-1] == "True True"
