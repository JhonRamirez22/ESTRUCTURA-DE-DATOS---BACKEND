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


@pytest.mark.asyncio
async def test_saturated_database_pool_sheds_load_with_503_not_500() -> None:
    """H: bajo sobrecarga el pool agotado daba 500 tras 10 s; debe ser 503 con Retry-After."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    api = FastAPI()
    api.add_middleware(HttpSecurityMiddleware)

    @api.get('/api/v1/saturado')
    async def saturated() -> None:
        raise PoolTimeout('QueuePool limit of size 10 overflow 10 reached')

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
        base_url='http://test',
    ) as client:
        response = await client.get('/api/v1/saturado')
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '2'
    assert 'QueuePool' not in response.text
    assert response.json()['error'].startswith('El servicio está saturado')


@pytest.mark.asyncio
async def test_inflight_limit_sheds_excess_requests_and_exempts_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.settings import get_settings

    monkeypatch.setenv('MAX_INFLIGHT_REQUESTS', '2')
    get_settings.cache_clear()
    release = asyncio.Event()
    api = FastAPI()
    api.add_middleware(HttpSecurityMiddleware)

    @api.get('/api/v1/lento')
    async def slow() -> dict[str, bool]:
        await release.wait()
        return {'ok': True}

    @api.get('/api/v1/health')
    async def health() -> dict[str, bool]:
        return {'ok': True}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url='http://test'
        ) as client:
            first = asyncio.create_task(client.get('/api/v1/lento'))
            second = asyncio.create_task(client.get('/api/v1/lento'))
            await asyncio.sleep(0.05)
            shed = await client.get('/api/v1/lento')
            assert shed.status_code == 503
            assert shed.headers['Retry-After'] == '1'
            # La salud no se descarta.
            assert (await client.get('/api/v1/health')).status_code == 200
            release.set()
            assert (await first).status_code == 200
            assert (await second).status_code == 200
            assert (await client.get('/api/v1/lento')).status_code == 200  # el cupo se libera
    finally:
        get_settings.cache_clear()
