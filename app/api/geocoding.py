"""Búsqueda de direcciones privada para el formulario de despacho."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import _configured_secret, require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.services.address_geocoder import (
    AddressGeocoder,
    AddressGeocoderError,
    get_address_geocoder,
)
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

router = APIRouter(prefix="/geocoding", tags=["direcciones"])
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}
_RATE_LIMITER = CustomerChatRateLimiter(max_requests=30, window_seconds=60)


@router.get("/search")
async def search_address(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    geocoder: Annotated[AddressGeocoder | None, Depends(get_address_geocoder)],
    q: Annotated[str, Query(min_length=3, max_length=160)],
) -> JSONResponse:
    secret = _configured_secret(get_settings())
    if secret is None:
        return _error("La búsqueda de direcciones no está configurada en el servidor.", 503)
    retry_after = await _RATE_LIMITER.retry_after_shared(
        session, f"address:{_claims['accountId']}", hmac_secret=secret,
    )
    if retry_after is not None:
        return JSONResponse(
            content={"error": "Demasiadas búsquedas; espera un momento y vuelve a intentar."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry_after)},
        )
    if geocoder is None:
        return _error("La búsqueda de direcciones no está configurada en el servidor.", 503)

    try:
        suggestions = await geocoder.search(q)
    except AddressGeocoderError as error:
        return _error(str(error), error.status_code)

    return JSONResponse(
        content={"suggestions": [suggestion.as_dict() for suggestion in suggestions]},
        headers=_NO_STORE,
    )


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": message},
        status_code=status_code,
        headers=_NO_STORE,
    )
