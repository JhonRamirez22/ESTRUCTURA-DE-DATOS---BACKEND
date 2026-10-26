"""Inteligencia de ubicaciones: Groq propone, el algoritmo verifica.

- Agrupación: el agrupamiento determinista (enlace simple por radio, con capacidad)
  define los grupos candidatos. Groq puede proponer grupos con nombre y motivo, pero
  solo se aceptan miembros dentro de un candidato, sin repetir paradas y con tamaño válido.
- Explicación: Groq redacta un resumen de la optimización. Cada cifra del texto debe
  aparecer en el resumen verificado; si no, se muestra el texto determinista.

Solo viajan alias (p-N, stop-N), distancias y minutos: nunca direcciones, nombres ni guías.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.location.proximity import Located, cluster_by_radius, haversine_m
from app.services.optimization_feed import record_incident
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
MAX_GROUP_SIZE = 6
# El agrupamiento compara todos los pares (O(n²)): se acota para no bloquear el servidor.
MAX_GROUP_INPUT = 300
GROUP_RADIUS_M = 400.0
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# Nombres con dígitos que no son cifras de tiempo: «2-opt», «3-opt», alias p-0 / stop-3.
_NOT_FIGURES = re.compile(r"\b(?:[23]-opt|(?:p|stop)-\d+)\b", re.IGNORECASE)
_GROUP_PROMPT = (
    "Eres un planificador de reparto. Recibes grupos candidatos de paradas con alias p-N y "
    "distancias en metros. Devuelve solo JSON con la forma "
    '{"groups":[{"members":["p-0","p-1"],"reason":"motivo breve"}]}. Usa solo alias de los '
    "candidatos, sin repetir paradas, de 2 a 6 por grupo. No agregues texto fuera del JSON."
)
_EXPLAIN_PROMPT = (
    "Explicas a un despachador por qué una ruta quedó como está. Recibes un resumen verificado "
    "con minutos. Responde solo JSON {\"text\":\"...\"} en español, máximo 3 frases. Usa solo "
    "las cifras del resumen; no inventes calles, cierres ni tiempos."
)


@dataclass(frozen=True, slots=True)
class PendingLocation:
    ident: str  # id real del pedido: nunca sale hacia la IA
    alias: str  # p-N
    lat: float
    lng: float


class IntelligenceUnavailable(RuntimeError):
    """La IA no está configurada o no respondió con algo utilizable."""


def _settings_credentials() -> tuple[str, str, str, int] | None:
    settings = get_settings()
    if not (settings.ai_allow_route_location_sharing or settings.ai_allow_location_data_sharing):
        return None
    if settings.ai_traffic_provider != "groq":
        return None
    key = settings.customer_chat_api_key
    api_url = (settings.customer_chat_api_url or "").strip()
    model = (settings.customer_chat_model or "").strip()
    api_key = key.get_secret_value().strip() if key is not None else ""
    if not api_url or not api_key or not model:
        return None
    return api_url, api_key, model, settings.ai_traffic_timeout_ms


async def _chat_json(
    system: str,
    payload: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    credentials = _settings_credentials()
    if credentials is None:
        raise IntelligenceUnavailable("La IA no está configurada o no tiene permiso de datos.")
    api_url, api_key, model, timeout_ms = credentials
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": 500,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000, transport=transport) as client:
            response = await client.post(
                api_url, headers={"Authorization": f"Bearer {api_key}"}, json=body
            )
    except httpx.HTTPError as error:
        record_incident("groq", "inteligencia:red")
        raise IntelligenceUnavailable("No se pudo consultar a la IA.") from error
    if not response.is_success:
        record_incident("groq", f"inteligencia:http-{response.status_code}")
        raise IntelligenceUnavailable(f"La IA respondió HTTP {response.status_code}.")
    try:
        content = str(response.json()["choices"][0]["message"]["content"]).strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise IntelligenceUnavailable("La respuesta de la IA no es JSON válido.") from error
    if not isinstance(parsed, dict):
        raise IntelligenceUnavailable("La respuesta de la IA no es un objeto.")
    return parsed


def build_groups(pending: Sequence[PendingLocation]) -> list[dict[str, Any]]:
    """Grupos candidatos deterministas, con distancias entre miembros en metros."""
    pending = list(pending)[:MAX_GROUP_INPUT]
    located = [Located(point.alias, point.lat, point.lng) for point in pending]
    by_alias = {point.alias: point for point in pending}
    groups = cluster_by_radius(located, GROUP_RADIUS_M, MAX_GROUP_SIZE)
    result: list[dict[str, Any]] = []
    for members in groups:
        points = [by_alias[alias] for alias in members]
        spread = max(
            (haversine_m(a.lat, a.lng, b.lat, b.lng) for a in points for b in points), default=0.0
        )
        result.append(
            {
                "members": members,
                "spreadMeters": round(spread),
                "source": "deterministic",
                "reason": None,
            }
        )
    return result


async def suggest_groups(
    pending: Sequence[PendingLocation], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Grupos finales: los que Groq propone y el chequeo acepta, más los deterministas restantes."""
    candidates = build_groups(pending)
    status: dict[str, Any] = {"source": "deterministic", "proposed": False, "accepted": 0}
    if not candidates:
        return {"groups": [], "ai": status}

    payload = {
        "groups": [
            {"members": group["members"], "spreadMeters": group["spreadMeters"]}
            for group in candidates
        ],
        "maxGroupSize": MAX_GROUP_SIZE,
    }
    accepted_groups: list[dict[str, Any]] = []
    try:
        parsed = await _chat_json(_GROUP_PROMPT, payload, transport=transport)
        status["proposed"] = True
        accepted_groups = _validate_group_proposal(parsed, candidates)
        status["accepted"] = len(accepted_groups)
        status["source"] = "groq-validated" if accepted_groups else "deterministic"
    except IntelligenceUnavailable as error:
        status["reason"] = str(error)

    covered = {alias for group in accepted_groups for alias in group["members"]}
    leftovers = [
        group for group in candidates
        if not set(group["members"]) & covered
    ]
    return {"groups": accepted_groups + leftovers, "ai": status}


def _validate_group_proposal(
    parsed: Mapping[str, Any], candidates: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Solo acepta grupos que caen dentro de un candidato, sin repetir ni inventar paradas."""
    raw_groups = parsed.get("groups")
    if not isinstance(raw_groups, list):
        return []
    allowed = [set(group["members"]) for group in candidates]
    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            continue
        members = item.get("members")
        if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
            continue
        unique = list(dict.fromkeys(members))
        if not (2 <= len(unique) <= MAX_GROUP_SIZE) or len(unique) != len(members):
            continue
        if any(member in seen for member in unique):
            continue
        if not any(set(unique) <= candidate for candidate in allowed):
            continue
        seen.update(unique)
        reason = item.get("reason")
        accepted.append(
            {
                "members": unique,
                "spreadMeters": next(
                    g["spreadMeters"] for g in candidates if set(unique) <= set(g["members"])
                ),
                "source": "groq-validated",
                "reason": str(reason)[:200] if isinstance(reason, str) else None,
            }
        )
    return accepted


def verify_explanation(text: str, summary_numbers: Sequence[float]) -> bool:
    """Cada cifra del texto debe coincidir con una cifra del resumen verificado."""
    found = [
        float(match.replace(",", "."))
        for match in _NUMBER.findall(_NOT_FIGURES.sub(" ", text))
    ]
    if not found:
        return True
    tolerance = 0.06
    return all(
        any(math.isclose(value, known, abs_tol=tolerance) for known in summary_numbers)
        for value in found
    )


def deterministic_explanation(summary: Mapping[str, Any]) -> str:
    saved = summary.get("savedMinutes")
    ladder = {step["step"]: step.get("minutes") for step in summary.get("ladder", [])}
    if saved is None or saved <= 0.05:
        return "El orden base ya era el mejor con los tiempos medidos; no hubo cambios."
    base = ladder.get("nearestNeighbor")
    final = ladder.get("aiOrder") or ladder.get("orOpt")
    if base is None or final is None:
        return f"La ruta optimizada ahorra {round(saved, 1)} min frente al orden base."
    return (
        f"El orden base tardaba {base} min y el final {final} min, "
        f"un ahorro de {round(saved, 1)} min con la misma matriz de tiempos."
    )


async def explain_route(
    summary: Mapping[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Texto explicativo de Groq, validado contra las cifras del resumen."""
    numbers: list[float] = []
    for step in summary.get("ladder", []):
        if step.get("minutes") is not None:
            numbers.append(float(step["minutes"]))
    if summary.get("savedMinutes") is not None:
        numbers.append(float(summary["savedMinutes"]))
    tomtom = summary.get("tomtom", {})
    numbers.extend(
        float(tomtom.get(key, 0)) for key in ("sampled", "adjusted", "closed", "segments")
    )
    fallback = deterministic_explanation(summary)
    try:
        payload = {
            "ladder": [
                {"step": step["step"], "minutes": step.get("minutes")}
                for step in summary.get("ladder", [])
            ],
            "savedMinutes": summary.get("savedMinutes"),
            "tomtom": tomtom,
            "groq": {"orderAccepted": summary.get("groq", {}).get("orderAccepted")},
        }
        parsed = await _chat_json(_EXPLAIN_PROMPT, payload, transport=transport)
        text = parsed.get("text")
    except IntelligenceUnavailable:
        return {"text": fallback, "source": "deterministic"}
    if isinstance(text, str) and text.strip() and verify_explanation(text, numbers):
        return {"text": text.strip()[:600], "source": "groq-validated"}
    record_incident("groq", "inteligencia:texto-no-verificado")
    return {"text": fallback, "source": "deterministic"}
