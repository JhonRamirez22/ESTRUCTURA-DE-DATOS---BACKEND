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

    assert result.normalized_address == "Carrera 25 # 4 sur 65"
    assert request_body["task"] == "address_normalization"
    assert request_body["model"] == "test-model"
    assert request_body["input"]["coordinates"] == {"lat": 1.2136, "lng": -77.2811}  # type: ignore[index]


@pytest.mark.asyncio
async def test_openai_chat_protocol_validates_json_content() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_address_response())}}]},
        )

    advisor = HttpAddressAiAdvisor(
        api_url="https://model.example/v1/chat/completions",
        api_key=None,
        model="test-model",
        timeout_ms=200,
        transport=httpx.MockTransport(respond),
    )
    result = await advisor.normalize_address(
        AddressAiRequest(
            raw_address="cra 25 num 4 sur 65",
            coordinates={"lat": 1.2136, "lng": -77.2811},
        )
    )
    assert result.city == "Pasto"


@pytest.mark.asyncio
async def test_http_errors_are_sanitized_and_address_normalizer_falls_back() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="private provider diagnostics")

    advisor = HttpAddressAiAdvisor(
        api_url="https://model.example/v1/normalize",
        api_key=None,
        model=None,
        timeout_ms=200,
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(AddressAiError, match="límite") as raised:
        await advisor.normalize_address(
            AddressAiRequest(
                raw_address="cra 25 num 4 sur 65",
                coordinates={"lat": 1.2136, "lng": -77.2811},
            )
        )
    assert "private provider diagnostics" not in str(raised.value)

    result = await normalize_address(
        "cra 25 num 4 sur 65", 1.2136, -77.2811, _FailingAdvisor()
    )
    assert result.address == "Carrera 25 # 4 sur 65"
    assert result.source == "deterministic"
    assert "no respondió" in (result.warning or "")


@pytest.mark.asyncio
async def test_http_timeout_is_explicit_and_contains_no_provider_payload() -> None:
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private address diagnostics", request=request)

    advisor = HttpAddressAiAdvisor(
        api_url="https://model.example/v1/normalize",
        api_key=None,
        model=None,
        timeout_ms=200,
        transport=httpx.MockTransport(timeout),
    )

    with pytest.raises(AddressAiError, match="tiempo límite") as raised:
        await advisor.normalize_address(
            AddressAiRequest(
                raw_address="Carrera 25 # 4 sur 65",
                coordinates={"lat": 1.2136, "lng": -77.2811},
            )
        )
    assert "private address diagnostics" not in str(raised.value)


def test_location_data_sharing_is_opt_in() -> None:
    disabled = Settings(
        _env_file=None,
        ai_allow_location_data_sharing=False,
        ai_traffic_api_url="https://model.example",
    )
    enabled_without_endpoint = Settings(_env_file=None, ai_allow_location_data_sharing=True)
    configured = Settings(
        _env_file=None,
        ai_allow_location_data_sharing=True,
        ai_traffic_api_url="https://model.example/v1/normalize",
        ai_traffic_api_key="test-key",
        ai_traffic_model="test-model",
    )

    assert create_address_ai_advisor(disabled) is None
    assert create_address_ai_advisor(enabled_without_endpoint) is None
    assert isinstance(create_address_ai_advisor(configured), HttpAddressAiAdvisor)


class _FailingAdvisor:
    async def normalize_address(self, _request: AddressAiRequest) -> AddressAiResponse:
        raise AddressAiError("fallo con datos privados ocultos")
