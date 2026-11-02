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
