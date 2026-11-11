"""Resistencia a abuso: el bloqueo de un atacante no debe bloquear a los demás.

Hipótesis confirmadas con la API real (ver auditoría de seguridad):
- H1: el límite de login se aplicaba por correo, así que cualquiera que conociera el correo del
  despachador podía mantenerlo fuera con pocas peticiones por minuto.
- H2: el límite de solicitudes públicas tomaba el primer valor de X-Forwarded-For, que el cliente
  controla; con valores distintos agotaba el cupo global y bloqueaba a los clientes reales.
- H3: el backend aceptaba peticiones directas aunque debía hablar solo con Vercel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import auth, order_requests
from app.core.passwords import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import Account
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

SECRET = "abuse-resistance-secret-with-more-than-32-bytes"
PASSWORD = "Una contraseña privada 2026"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'abuse.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(
            Account(email="despacho@example.com", password_hash=hash_password(PASSWORD),
                    role="dispatcher")
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    monkeypatch.setenv("NODE_ENV", "test")
    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    # Contadores limpios: los limitadores viven en memoria del proceso.
    monkeypatch.setattr(
        auth, "_LOGIN_LIMITER", CustomerChatRateLimiter(max_requests=10, window_seconds=60)
    )
    per_client = CustomerChatRateLimiter(max_requests=3, window_seconds=600)
    service = CustomerChatRateLimiter(max_requests=20, window_seconds=600)
    monkeypatch.setattr(order_requests, "_CLIENT_LIMITER", per_client)
    monkeypatch.setattr(order_requests, "_SERVICE_LIMITER", service)
    monkeypatch.setattr(
        auth, "_LOGIN_EMAIL_LIMITER", CustomerChatRateLimiter(max_requests=60, window_seconds=600)
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Origin": "http://test"},
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        get_settings.cache_clear()


async def _login(client: httpx.AsyncClient, password: str, forwarded: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": forwarded},
        json={"email": "despacho@example.com", "password": password},
    )


@pytest.mark.asyncio
async def test_h1_un_atacante_no_bloquea_el_login_del_despachador(
    client: httpx.AsyncClient,
) -> None:
    for _ in range(15):
        await _login(client, "contraseña-incorrecta-123", "203.0.113.50")
    assert (await _login(client, "contraseña-incorrecta-123", "203.0.113.50")).status_code == 429
    legit = await _login(client, PASSWORD, "198.51.100.7")
    assert legit.status_code == 200, legit.text


@pytest.mark.asyncio
async def test_h1_el_mismo_cliente_sigue_limitado_aunque_acierte(
    client: httpx.AsyncClient,
) -> None:
    for _ in range(10):
        await _login(client, "contraseña-incorrecta-123", "203.0.113.50")
    assert (await _login(client, PASSWORD, "203.0.113.50")).status_code == 429


@pytest.mark.asyncio
async def test_h1_la_fuerza_bruta_distribuida_encuentra_un_tope_por_correo(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth, "_LOGIN_EMAIL_LIMITER", CustomerChatRateLimiter(max_requests=5, window_seconds=600)
    )
    codes = [
        (await _login(client, "contraseña-incorrecta-123", f"192.0.2.{index}")).status_code
        for index in range(1, 9)
    ]
    assert codes[:5] == [401] * 5
    assert set(codes[5:]) == {429}


def _request_body() -> dict[str, object]:
    return {"address": "Calle 18 # 22-14", "lat": 1.2136, "lng": -77.2811}


@pytest.mark.asyncio
async def test_h2_x_forwarded_for_falsificado_no_evade_el_limite_por_cliente(
    client: httpx.AsyncClient,
) -> None:
    # El cliente antepone valores inventados; el último salto lo añade la infraestructura.
    codes = []
    for index in range(6):
        response = await client.post(
            "/api/v1/solicitudes",
            headers={"X-Forwarded-For": f"10.9.9.{index}, 198.51.100.20"},
            json=_request_body(),
        )
        codes.append(response.status_code)
    assert codes.count(201) == 3, codes
    assert set(codes[3:]) == {429}


@pytest.mark.asyncio
async def test_h2_clientes_distintos_detras_de_la_infraestructura_no_se_estorban(
    client: httpx.AsyncClient,
) -> None:
    for index in range(3):
        first = await client.post(
            "/api/v1/solicitudes",
            headers={"X-Forwarded-For": f"203.0.113.{index}, 198.51.100.20"},
            json=_request_body(),
        )
        assert first.status_code in {201, 429}
    other = await client.post(
        "/api/v1/solicitudes",
        headers={"X-Forwarded-For": "198.51.100.99"},
        json=_request_body(),
    )
    assert other.status_code == 201


@pytest.mark.asyncio
async def test_h3_con_candado_de_origen_solo_pasa_el_proxy_autorizado(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "origen-compartido-con-el-proxy-de-vercel-32b")
    get_settings.cache_clear()
    direct = await client.get("/api/v1/seguimiento", params={"guia": "x"})
    assert direct.status_code == 403
    assert direct.json()["error"] == "Origen no autorizado."
    wrong = await client.get(
        "/api/v1/seguimiento", params={"guia": "x"}, headers={"X-Origin-Auth": "otro-valor"}
    )
    assert wrong.status_code == 403
    allowed = await client.get(
        "/api/v1/seguimiento",
        params={"guia": "x"},
        headers={"X-Origin-Auth": "origen-compartido-con-el-proxy-de-vercel-32b"},
    )
    assert allowed.status_code in {400, 404}
    # El balanceador comprueba la salud sin conocer el secreto.
    assert (await client.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
async def test_h3_con_candado_se_confia_en_el_primer_salto_que_escribe_vercel(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "origen-compartido-con-el-proxy-de-vercel-32b"
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", secret)
    get_settings.cache_clear()
    codes = []
    for index in range(4):
        response = await client.post(
            "/api/v1/solicitudes",
            headers={
                "X-Origin-Auth": secret,
                "X-Forwarded-For": f"203.0.113.{index}, 76.76.21.21",
            },
            json=_request_body(),
        )
        codes.append(response.status_code)
    assert codes == [201, 201, 201, 201]  # cada cliente real tiene su propio contador
