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
