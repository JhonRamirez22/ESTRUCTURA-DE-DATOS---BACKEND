"""Sugerencias en contexto para el despachador: el cálculo decide, la IA solo redacta.

Cada sugerencia sale de una regla determinista sobre el estado real (repartidores y pedidos
pendientes) y trae su cifra verificada y, si procede, una acción de un clic. Groq puede
reescribir el texto, pero solo se acepta si cada cifra del texto es una de las verificadas y
el nombre del repartidor sigue siendo el marcador ``{courier}``: los nombres y direcciones
nunca viajan a la IA. Si Groq falla, no está configurado o se pasó del tope, se muestra el
texto determinista, que dice lo mismo.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.core.location.proximity import haversine_m
from app.models.enums import CourierStatus
from app.services.location_intelligence import (
    IntelligenceUnavailable,
    _chat_json,
    verify_explanation,
)
from app.services.optimization_feed import record_incident

MAX_SUGGESTIONS = 5
MAX_ORDERS_PER_ACTION = 3
NEARBY_RADIUS_M = 3_000.0
FRESH_MINUTES = 2.0
STALE_ON_ROUTE_MINUTES = 10.0
AI_CALLS_PER_DAY = 200

_PLACEHOLDER = "{courier}"
_PHRASE_PROMPT = (
    "Redactas avisos breves para un despachador de mensajería. Recibes una lista de avisos "
    "con 'id', 'kind' y 'figures' verificadas. Devuelve solo JSON "
    '{"texts":{"<id>":"frase"}} en español, una frase corta por aviso. Si el aviso nombra al '
    "repartidor, escribe exactamente {courier} donde iría su nombre. Usa solo las cifras dadas; "
    "no inventes calles, causas ni tiempos."
)


@dataclass(frozen=True, slots=True)
class CourierView:
    ident: str
    name: str
    status: CourierStatus
    lat: float | None
    lng: float | None
    last_location_at: datetime | None


@dataclass(frozen=True, slots=True)
class PendingView:
    ident: str
    lat: float
    lng: float


def _minutes_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds() / 60.0)


def build_suggestions(
    couriers: Sequence[CourierView], pending: Sequence[PendingView], now: datetime
) -> list[dict[str, Any]]:
    """Sugerencias deterministas, las más urgentes primero. No llama a ningún servicio."""
    found: list[tuple[int, dict[str, Any]]] = []
    for courier in couriers:
        age = _minutes_since(courier.last_location_at, now)
        has_position = courier.lat is not None and courier.lng is not None
        if courier.status == CourierStatus.ON_ROUTE and (
            age is None or age >= STALE_ON_ROUTE_MINUTES
        ):
            minutes = None if age is None else round(age)
            text = (
                f"{courier.name} va en ruta pero no ha enviado ubicación."
                if minutes is None
                else f"{courier.name} va en ruta y no envía ubicación hace {minutes} min."
            )
            found.append((0, _card(courier, "stale_gps", "warning", text, {"minutes": minutes})))
        elif courier.status == CourierStatus.AVAILABLE and (
            not has_position or age is None or age > FRESH_MINUTES
        ):
            if pending:
                found.append((
                    2,
                    _card(
                        courier,
                        "no_position",
                        "info",
                        f"{courier.name} está libre pero sin ubicación reciente: "
                        "no entrará en el reparto.",
                        {},
                    ),
                ))
        elif courier.status == CourierStatus.AVAILABLE and has_position and pending:
            lat, lng = courier.lat or 0.0, courier.lng or 0.0
            ranked = sorted(
                ((haversine_m(lat, lng, p.lat, p.lng), p) for p in pending), key=lambda x: x[0]
            )
            near = [item for item in ranked if item[0] <= NEARBY_RADIUS_M][:MAX_ORDERS_PER_ACTION]
            if near:
                nearest = round(near[0][0])
                text = (
                    f"{courier.name} está libre y tiene {len(near)} "
                    f"{'pedido' if len(near) == 1 else 'pedidos'} cerca "
                    f"(el más próximo a {nearest} m)."
                )
                card = _card(
                    courier, "idle_courier", "action", text,
                    {"orders": len(near), "nearestMeters": nearest},
                )
                card["action"] = {
                    "type": "select",
                    "label": "Seleccionar esos pedidos",
                    "deliveryPointIds": [p.ident for _, p in near],
                }
                found.append((1, card))
    found.sort(key=lambda item: (item[0], item[1]["id"]))
    return [card for _, card in found[:MAX_SUGGESTIONS]]


def _card(
    courier: CourierView, kind: str, severity: str, text: str, figures: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": f"{kind}:{courier.ident}",
        "kind": kind,
        "severity": severity,
        "courierId": courier.ident,
        "text": text,
        "figures": figures,
        "source": "deterministic",
        "action": None,
        "_name": courier.name,
    }


class DailyBudget:
    """Tope de llamadas a la IA por día: al agotarse, todo sigue con texto determinista."""

    def __init__(self, limit: int, clock: Any = time.time) -> None:
        self._limit = limit
        self._clock = clock
        self._day = -1
        self._used = 0

    def take(self) -> bool:
        day = int(self._clock() // 86_400)
        if day != self._day:
            self._day, self._used = day, 0
        if self._used >= self._limit:
            return False
        self._used += 1
        return True


_BUDGET = DailyBudget(AI_CALLS_PER_DAY)


def _figure_values(figures: dict[str, Any]) -> list[float]:
    return [float(value) for value in figures.values() if isinstance(value, int | float)]


async def phrase_with_ai(
    cards: list[dict[str, Any]],
    *,
    budget: DailyBudget | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reescribe los textos con Groq si es posible; devuelve tarjetas públicas y el estado."""
    status: dict[str, Any] = {"source": "deterministic", "accepted": 0}
    result = [dict(card) for card in cards]
    if result:
        gate = budget if budget is not None else _BUDGET
        if not gate.take():
            status["reason"] = "Se alcanzó el tope diario de la IA."
        else:
            try:
                payload = {
                    "alerts": [
                        {
                            "id": c["id"],
                            "kind": c["kind"],
                            "figures": c["figures"],
                            "namesCourier": "_name" in c,
                        }
                        for c in result
                    ]
                }
                parsed = await _chat_json(
                    _PHRASE_PROMPT, payload, transport=transport, text_only=True
                )
                texts = parsed.get("texts")
                if isinstance(texts, dict):
                    for card in result:
                        text = texts.get(card["id"])
                        if _acceptable(text, card):
                            card["text"] = _with_courier(str(text).strip(), card)[:240]
                            card["source"] = "groq-validated"
                            status["accepted"] += 1
                    if status["accepted"]:
                        status["source"] = "groq-validated"
            except IntelligenceUnavailable as error:
                status["reason"] = str(error)
    for card in result:
        card.pop("_name", None)
    return result, status


def _with_courier(text: str, card: dict[str, Any]) -> str:
    """Pone el nombre del repartidor en el texto, o al inicio si el modelo no dejó el marcador."""
    name = card.get("_name")
    if name is None:
        return text
    if _PLACEHOLDER in text:
        return text.replace(_PLACEHOLDER, str(name))
    return f"{name}: {text}"


def _acceptable(text: object, card: dict[str, Any]) -> bool:
    if not isinstance(text, str) or not text.strip() or len(text) > 240:
        return False
    # El marcador es opcional con repartidor (si falta, el nombre se antepone) y sobra sin él.
    if text.count(_PLACEHOLDER) > (1 if "_name" in card else 0):
        return False
    if not verify_explanation(text.replace(_PLACEHOLDER, ""), _figure_values(card["figures"])):
        record_incident("groq", "sugerencias:texto-no-verificado")
        return False
    return True
