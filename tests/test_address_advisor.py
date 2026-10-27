from __future__ import annotations

import json

import httpx
import pytest

from app.services.address_advisor import (
    AddressAiError,
    AddressAiRequest,
    AddressAiResponse,
    HttpAddressAiAdvisor,
    create_address_ai_advisor,
)
from app.services.address_normalization import normalize_address
from app.settings import Settings


def _address_response(address: str = "Carrera 25 # 4 sur 65") -> dict[str, object]:
    return {
        "normalizedAddress": address,
        "isInPasto": True,
        "confidence": 0.96,
        "city": "Pasto",
        "department": "Nariño",
        "country": "Colombia",
        "reason": "Abreviatura local normalizada.",
    }


@pytest.mark.asyncio
async def test_http_advisor_posts_contract_to_mock_transport_without_real_network() -> None:
    request_body: dict[str, object] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(200, json=_address_response())

    advisor = HttpAddressAiAdvisor(
        api_url="https://model.example/v1/normalize",
        api_key="test-secret",
        model="test-model",
        timeout_ms=200,
        transport=httpx.MockTransport(respond),
    )
    result = await advisor.normalize_address(
        AddressAiRequest(
            raw_address="Carrera 25 # 4 sur 65",
            coordinates={"lat": 1.2136, "lng": -77.2811},
        )
    )

