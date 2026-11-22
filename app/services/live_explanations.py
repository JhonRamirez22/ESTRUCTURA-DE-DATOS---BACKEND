"""Explains, in plain Spanish, what the live optimisation did and why.

Every explanation starts as a deterministic sentence built from verified figures. Groq may
rephrase it, but only when every number in the new text is one of the verified figures and the
placeholders (``{stop}``, ``{route}``, ``{from}``, ``{to}``) are kept: names and addresses never
leave the server. If the AI is unavailable, slow or invents something, the deterministic text is
shown, which says the same thing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.structures.ttl_cache import TtlCache
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import Fleet
from app.routing.reoptimizer import FleetProposal
from app.services.dispatch_suggestions import AI_CALLS_PER_DAY, DailyBudget
from app.services.location_intelligence import (
    IntelligenceUnavailable,
    _chat_json,
    verify_explanation,
)

PLACEHOLDERS = ("{stop}", "{route}", "{from}", "{to}")
PHRASE_TIMEOUT_SECONDS = 4.0
MIN_REORDER_GAIN = 0.2
_PROMPT = (
    "Redactas explicaciones breves para un despachador de mensajería sobre una optimización de "
    "rutas. Recibes una lista con 'id', 'kind', 'figures' (cifras verificadas) y 'draft' (una "
    "frase base). Devuelve solo JSON {\"texts\":{\"<id>\":\"frase\"}} en español, una frase corta "
    "por elemento que explique POR QUÉ ayudó el cambio. Conserva exactamente los marcadores "
    "{stop}, {route}, {from} y {to} que aparezcan en 'draft'. Usa solo las cifras dadas; no "
    "inventes calles, causas, tiempos ni nombres."
)


@dataclass(slots=True)
class Explanation:
    ident: str
    kind: str
    text: str
    figures: dict[str, float]
    route_id: str | None = None
    stop_id: str | None = None
    from_route_id: str | None = None
    to_route_id: str | None = None
    source: str = "deterministic"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.ident,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "routeId": self.route_id,
            "stopId": self.stop_id,
            "fromRouteId": self.from_route_id,
            "toRouteId": self.to_route_id,
        }


@dataclass(frozen=True, slots=True)
class NextStop:
    route_id: str
    stop_id: str
    eta_minutes: float
    focus: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "routeId": self.route_id,
            "stopId": self.stop_id,
            "etaMinutes": round(self.eta_minutes, 1),
            "focus": self.focus,
        }


def _n(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


class NextStopFinder:
    """Finds each route's next stop and the one that is closest of the whole fleet."""

    def __init__(self, fleet: Fleet, matrix: CostMatrix) -> None:
        self._fleet = fleet
        self._matrix = matrix

    def find(self) -> list[NextStop]:
        index = self._fleet.index()
        found: list[tuple[str, str, float]] = []
        for route in self._fleet.routes:
            if not route.stops:
                continue
            first = route.stops[0]
            eta = self._matrix.cost(
                index.courier_node[route.route_id], index.stop_node[first.stop_id]
            )
            found.append((route.route_id, first.stop_id, eta))
        reachable = [item for item in found if math.isfinite(item[2])]
        best = min(reachable, key=lambda item: item[2])[0] if reachable else None
        return [NextStop(r, s, e if math.isfinite(e) else 0.0, r == best) for r, s, e in found]


class LiveExplainer:
    """Builds the deterministic explanations for one proposal."""

    def build(self, proposal: FleetProposal, next_stops: Sequence[NextStop]) -> list[Explanation]:
        out: list[Explanation] = []
        ordered = sorted(next_stops, key=lambda item: item.eta_minutes)
        for item in ordered:
            suffix = " Es la entrega más próxima de toda la flota." if item.focus else ""
            out.append(Explanation(
                f"next:{item.route_id}", "next",
                f"{{route}} va primero a {{stop}} (a unos {_n(item.eta_minutes)} min): es su "
                f"parada más cercana ahora.{suffix}",
                {"eta": round(item.eta_minutes, 1)}, route_id=item.route_id, stop_id=item.stop_id,
            ))
        for move in proposal.moves:
            out.append(Explanation(
                f"move:{move.stop_id}", "move",
                f"{{stop}} pasa de {{from}} a {{to}}: {{to}} llega antes y la flota ahorra "
                f"{_n(move.gain_minutes)} min.",
                {"gain": round(move.gain_minutes, 1)},
                stop_id=move.stop_id, from_route_id=move.from_route, to_route_id=move.to_route,
            ))
        for route in proposal.routes:
            if route.moved_in or route.moved_out or route.saved_minutes < MIN_REORDER_GAIN:
                continue
            out.append(Explanation(
                f"reorder:{route.route_id}", "reorder",
                f"Reordené la ruta de {{route}}: baja de {_n(route.current_minutes)} a "
                f"{_n(route.proposed_minutes)} min (−{_n(route.saved_minutes)} min) con un orden "
                "de visita más corto.",
                {
                    "from": round(route.current_minutes, 1),
                    "to": round(route.proposed_minutes, 1),
                    "saved": round(route.saved_minutes, 1),
                },
                route_id=route.route_id,
            ))
        before, after = proposal.score_before, proposal.score_after
        if after.crossings < before.crossings:
            out.append(Explanation(
                "crossings", "crossings",
                f"Se quitan {before.crossings - after.crossings} cruces entre recorridos: las "
                "rutas ya no se pisan.",
                {"removed": float(before.crossings - after.crossings)},
            ))
        if after.longest_minutes < before.longest_minutes - 0.05:
            out.append(Explanation(
                "balance", "balance",
                f"La ruta más larga baja de {_n(before.longest_minutes)} a "
                f"{_n(after.longest_minutes)} min: el reparto queda más parejo.",
                {"from": round(before.longest_minutes, 1), "to": round(after.longest_minutes, 1)},
            ))
        if not proposal.changed:
            out.append(Explanation(
                "stable", "stable",
                f"Revisé {len(proposal.routes)} rutas con el tráfico de ahora y el plan actual ya "
                "es el mejor que encontré.",
                {"routes": float(len(proposal.routes))},
            ))
        return out


class ExplanationPhraser:
    """Optionally rewrites explanations with Groq, accepting only verified text."""

    def __init__(
        self,
        budget: DailyBudget | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: TtlCache[str, dict[str, str]] | None = None,
    ) -> None:
        self._budget = budget if budget is not None else _BUDGET
        self._transport = transport
        self._cache = cache if cache is not None else _PHRASE_CACHE

    async def phrase(self, items: list[Explanation]) -> str:
        """Updates ``items`` in place; returns ``ai``, ``cached`` or ``deterministic``."""
        candidates = [item for item in items if item.kind in _PHRASED_KINDS]
        if not candidates:
            return "deterministic"
        signature = hashlib.sha256(
            json.dumps([(c.ident, c.text) for c in candidates], ensure_ascii=False).encode()
        ).hexdigest()
        known = self._cache.get(signature)
        if known is None:
            if not self._budget.take():
                return "deterministic"
            payload = {"items": [
                {"id": c.ident, "kind": c.kind, "figures": c.figures, "draft": c.text}
                for c in candidates
            ]}
            try:
                parsed = await asyncio.wait_for(
                    _chat_json(_PROMPT, payload, transport=self._transport, text_only=True),
                    PHRASE_TIMEOUT_SECONDS,
                )
            except (IntelligenceUnavailable, TimeoutError):
                return "deterministic"
            texts = parsed.get("texts")
            known = {}
            if isinstance(texts, dict):
                for item in candidates:
                    text = texts.get(item.ident)
                    if self._acceptable(text, item):
                        known[item.ident] = str(text).strip()[:240]
            self._cache.put(signature, known)
            fresh = True
        else:
            fresh = False
        for item in candidates:
            if item.ident in known:
                item.text, item.source = known[item.ident], "groq-validated"
        if not known:
            return "deterministic"
        return "ai" if fresh else "cached"

    @staticmethod
    def _acceptable(text: object, item: Explanation) -> bool:
        if not isinstance(text, str) or not text.strip() or len(text) > 240:
            return False
        for marker in PLACEHOLDERS:
            if text.count(marker) > item.text.count(marker):
                return False
        plain = text
        for marker in PLACEHOLDERS:
            plain = plain.replace(marker, "")
        return verify_explanation(plain, list(item.figures.values()))


_PHRASED_KINDS = {"move", "reorder", "balance", "crossings"}
_BUDGET = DailyBudget(AI_CALLS_PER_DAY)
_PHRASE_CACHE: TtlCache[str, dict[str, str]] = TtlCache(64, 600.0)
