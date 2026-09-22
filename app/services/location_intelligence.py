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
TEXT_MAX_TOKENS = 1500
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
