"""Resuelve texto sin exigir un pin previo; disponible solo para despacho."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _configured_secret, require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_geocoder import (
    AddressGeocoder,
    AddressGeocoderError,
    get_address_geocoder,
)
from app.services.address_resolver import resolve_address
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

router = APIRouter(prefix="/direcciones", tags=["direcciones"])
_LIMITER = CustomerChatRateLimiter(max_requests=30, window_seconds=60)


class ResolveAddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=3, max_length=160)


@router.post("/resolver")
async def resolve(
    body: ResolveAddressRequest,
    claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    geocoder: Annotated[AddressGeocoder | None, Depends(get_address_geocoder)],
    advisor: Annotated[AddressAiAdvisor | None, Depends(get_address_ai_advisor)],
) -> dict[str, object]:
    secret = _configured_secret(get_settings())
    if secret is None or geocoder is None:
        raise HTTPException(503, "La búsqueda de direcciones no está configurada.")
    retry = await _LIMITER.retry_after_shared(
        session, f"address:{claims['accountId']}", hmac_secret=secret,
    )
    if retry is not None:
        raise HTTPException(429, "Espera un momento antes de buscar otra dirección.",
                            headers={"Retry-After": str(retry)})
    try:
        return await resolve_address(body.address, geocoder, advisor)
    except AddressGeocoderError as error:
        raise HTTPException(error.status_code, str(error)) from error
