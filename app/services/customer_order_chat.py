"""Asistente de IA con contexto mínimo y alcance exclusivo al pedido consultado."""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.schemas.customer_chat import CustomerChatTurn
from app.settings import Settings, get_settings

_LOGGER = logging.getLogger(__name__)
_SCOPE_TERMS = (
    "pedido",
    "envio",
    "entrega",
    "guia",
    "paquete",
    "repartidor",
    "seguimiento",
)
_ORDER_SCOPE_PHRASES = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:ya\s+)?(?:fue\s+)?asignad[oa]\b",
        r"\b(?:ya\s+)?viene\b",
        r"\b(?:ya\s+)?salio\b",
        r"\b(?:por\s+)?donde\s+(?:va|esta)\b",
        r"\bcuanto\s+(?:falta|tarda)\b",
        r"\ba\s+que\s+hora\s+(?:llega|entrega|pasa)\b",
    )
)
_ORDER_FOLLOW_UP_PHRASES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:y\s+)?(?:entonces\s+)?a\s+que\s+horas?$",
        r"^(?:y\s+)?(?:entonces\s+)?(?:cuando|donde|cuanto)$",
        r"^(?:y\s+)?eso\s+(?:para\s+)?(?:cuando|donde|cuanto)$",
    )
)
_UNSAFE_MIXED_REQUESTS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:ignora|omite|olvida|desobedece|anula|cambia)\b.{0,120}"
        r"\b(?:instrucciones|reglas|prompt|sistema|restricciones)\b",
        r"\b(?:dime|muestra|revela|imprime|copia|cual|que)\b.{0,80}"
        r"\b(?:api\s*key|clave|contrasena|password|prompt|instrucciones|secretos?)\b",
        r"\b(?:otro|otra|otros|otras)\s+(?:pedido|guia|cliente|repartidor|envio)s?\b",
        r"\b(?:cuentame|escribeme|inventame|recomiendame|hazme|explicame|ensename|"
        r"resuelve|traduce|ayudame)\b.{0,100}"
        r"\b(?:chiste|cancion|pelicula|serie|receta|restaurante|poema|integral|"
        r"ecuacion|tarea|clima|pronostico|hotel|vuelo|turismo)\b",
    )
)
_ETA_QUESTION_PHRASES = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bcuanto\s+(?:tiempo\s+)?(?:falta|demora|tarda)\b",
        r"\ba\s+que\s+hora\s+(?:llega|entrega|pasa)\b",
        r"\bcuando\s+(?:llega|llegara|entrega)\b",
    )
)
_ETA_FOLLOW_UP_PHRASES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:y\s+)?(?:entonces\s+)?a\s+que\s+horas?$",
        r"^(?:y\s+)?(?:entonces\s+)?cuanto\s+(?:falta|demora|tarda)$",
    )
)
_OUT_OF_SCOPE_REPLY = (
    "Solo puedo ayudarte con el estado y la entrega de este pedido. "
    "Pregúntame por su guía, asignación, recorrido o entrega."
)
