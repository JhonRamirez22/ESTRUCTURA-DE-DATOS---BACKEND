from __future__ import annotations

import json

import httpx
import pytest

from app.core.ai.route_traffic import (
    RouteTrafficAdjustment,
    RouteTrafficAiRequest,
    apply_route_traffic_adjustments,
    build_route_traffic_candidates,
    filter_route_traffic_adjustments,
    parse_route_traffic_response,
    select_route_traffic_candidates,
)
from app.services.route_traffic_advisor import (
    HttpRouteTrafficAdvisor,
    RouteTrafficAiError,
)


def _request() -> RouteTrafficAiRequest:
    coordinates = [(-77.2811, 1.2136), (-77.28, 1.214)]
    matrix = [[0, 5], [6, 0]]
    candidates = build_route_traffic_candidates(["stop-0", "stop-1"], coordinates, matrix)
    from datetime import UTC, datetime

    return RouteTrafficAiRequest(tuple(candidates), datetime.now(UTC))


def test_route_traffic_candidates_are_private_and_adjustments_are_bounded() -> None:
    request = _request()
    payload = request.as_payload()
    assert payload["courierId"] == "courier"
    assert "stop-0" in json.dumps(payload)
    assert "uuid" not in json.dumps(payload).lower()

    candidates = request.candidates
    advice = parse_route_traffic_response(
        {"adjustments": [{"fromStopId": "stop-0", "toStopId": "stop-1", "trafficMultiplier": 2}]}
    )
    supported, ignored = filter_route_traffic_adjustments(
        candidates,
        (*advice.adjustments, RouteTrafficAdjustment("foreign", "stop-0", 1.5)),
    )
    assert ignored == 1
    matrix = apply_route_traffic_adjustments([[0, 5], [6, 0]], ["stop-0", "stop-1"], supported)
    assert matrix == [[0, 10], [6, 0]]


def test_candidate_selection_prioritizes_baseline_and_caps_payload() -> None:
    coordinates = [(-77.2811 + index / 10_000, 1.2136) for index in range(8)]
    matrix = [
        [0 if source == target else source + target + 1 for target in range(8)]
        for source in range(8)
    ]
    candidates = build_route_traffic_candidates(
        [f"stop-{index}" for index in range(8)], coordinates, matrix
    )
    selected = select_route_traffic_candidates(
        candidates,
        ["stop-0", "stop-2", "stop-4"],
        max_candidates=5,
    )
    assert len(selected) == 5
    selected_pairs = {
        (candidate.from_stop_id, candidate.to_stop_id) for candidate in selected
    }
    assert {("stop-0", "stop-2"), ("stop-2", "stop-4")} <= selected_pairs


@pytest.mark.asyncio
async def test_http_advisor_supports_openai_chat_and_sanitizes_provider_errors() -> None:
    request = _request()
    seen_payload: dict[str, object] = {}

    async def success_handler(incoming: httpx.Request) -> httpx.Response:
        body = json.loads(incoming.content)
        seen_payload.update(body)
        content = json.dumps({"adjustments": [], "model": "test-model"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    advisor = HttpRouteTrafficAdvisor(
        api_url="https://ai.example/v1/chat/completions",
        api_key="secret-test-key",
        model="test-model",
        timeout_ms=1_000,
        transport=httpx.MockTransport(success_handler),
    )
    result = await advisor.get_route_advice(request)
    assert result.model == "test-model"
    assert "stop-0" in str(seen_payload)

    async def failure_handler(_incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="private provider response")

    failing_advisor = HttpRouteTrafficAdvisor(
        api_url="https://ai.example/v1/chat/completions",
        api_key=None,
        model="test-model",
        timeout_ms=1_000,
        transport=httpx.MockTransport(failure_handler),
    )
    with pytest.raises(RouteTrafficAiError, match="límite") as error:
        await failing_advisor.get_route_advice(request)
    assert "private provider response" not in str(error.value)
