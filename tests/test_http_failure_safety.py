from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI

from app.services.http_security import HttpSecurityMiddleware


@pytest.mark.asyncio
async def test_unhandled_failure_is_json_and_correlated_without_sensitive_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FastAPI()
    api.add_middleware(HttpSecurityMiddleware)

    @api.get('/api/v1/test/{identifier}')
    async def failing_endpoint(identifier: str) -> None:
        raise RuntimeError(f'private-address secret-token {identifier}')

    request_id = '25b10214-d4c9-4214-a683-fd5b7d297959'
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
            base_url='http://test',
        ) as client:
            response = await client.get(
                '/api/v1/test/private-id?address=private-address',
                headers={'X-Request-ID': request_id},
            )
    assert response.status_code == 500
    assert response.headers['Content-Type'].startswith('application/json')
    assert response.headers['X-Request-ID'] == request_id
    assert response.json()['requestId'] == request_id
    assert request_id in caplog.text
    assert '/api/v1/test/{identifier}' in caplog.text
    for sensitive in ('private-address', 'private-id', 'secret-token'):
        assert sensitive not in caplog.text
        assert sensitive not in response.text
