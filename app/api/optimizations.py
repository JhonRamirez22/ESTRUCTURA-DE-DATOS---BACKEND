"""Panel de optimizaciones del despachador: historial reciente y resumen con IA."""

from __future__ import annotations

import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.services.optimization_feed import (
    mark_summary_requested,
    recent_events,
    stats,
    summary_cooldown_remaining,
)
from app.settings import get_settings

router = APIRouter(prefix="/optimizaciones", tags=["optimizaciones"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}
_SUMMARY_PROMPT = (
    "Eres analista de despacho de reparto en Pasto. Resume en español, en máximo cuatro frases, "
    "las optimizaciones recientes usando solo los datos recibidos. Indica cuántas propuestas se "
    "aplicaron, el ahorro estimado y qué conviene vigilar. No inventes calles, direcciones ni "
    "repartidores; las rutas se identifican solo con su alias."
)


@router.get("/recientes")
async def recent_optimizations(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, object]:
    current = stats()
    return {
        "events": recent_events(limit),
        "stats": {
            "proposals": current.proposals,
            "cacheHits": current.cache_hits,
            "applied": current.applied,
            "trafficSegments": current.traffic_segments,
            "savedMinutes": current.saved_minutes,
        },
        "summaryCooldownSeconds": round(summary_cooldown_remaining(), 1),
    }


@router.post("/resumen")
async def summarize_optimizations(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
) -> dict[str, object]:
    """Pide a Groq un resumen de las optimizaciones; no cambia ninguna ruta."""
    remaining = summary_cooldown_remaining()
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Espera {round(remaining)} s antes de pedir otro resumen.",
            headers=_NO_STORE,
        )
    settings = get_settings()
    key = (
        settings.customer_chat_api_key.get_secret_value().strip()
        if settings.customer_chat_api_key
        else ""
    )
    if not key or not settings.customer_chat_api_url or not settings.customer_chat_model:
        raise HTTPException(
            status_code=503, detail="El resumen con IA no está configurado.", headers=_NO_STORE
        )
    mark_summary_requested()
    current = stats()
    payload = {
        "stats": {
            "proposals": current.proposals,
            "applied": current.applied,
            "savedMinutes": current.saved_minutes,
            "trafficSegments": current.traffic_segments,
        },
        "events": recent_events(20),
    }
    body = {
        "model": settings.customer_chat_model,
        "messages": [
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.customer_chat_timeout_ms / 1000) as client:
            response = await client.post(
                settings.customer_chat_api_url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as error:
        _LOGGER.warning(
            "No se pudo obtener el resumen de IA", extra={"error_type": type(error).__name__}
        )
        raise HTTPException(
            status_code=502, detail="La IA no respondió. Intenta más tarde.", headers=_NO_STORE
        ) from error
    if response.status_code == 429:
        raise HTTPException(
            status_code=429, detail="La IA alcanzó su límite. Intenta más tarde.", headers=_NO_STORE
        )
    if not response.is_success:
        raise HTTPException(status_code=502, detail="La IA devolvió un error.", headers=_NO_STORE)
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail="La respuesta de la IA no es válida.", headers=_NO_STORE
        ) from error
    summary = str(content).strip()[:800]
    return {"summary": summary, "cooldownSeconds": round(summary_cooldown_remaining(), 1)}
