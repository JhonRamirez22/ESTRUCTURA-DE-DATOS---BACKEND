"""Contratos del asistente público, limitado a una guía de pedido."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CustomerChatTurn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # El cliente manda preguntas previas del usuario, no respuestas falsificables del asistente.
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=700)


class CustomerChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    guide: str = Field(min_length=1, max_length=200)
    messages: list[CustomerChatTurn] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_conversation(self) -> CustomerChatRequest:
        if sum(len(message.content) for message in self.messages) > 3_000:
            raise ValueError("La conversación excede el límite permitido.")
        return self


class CustomerChatResponse(BaseModel):
    reply: str = Field(min_length=1, max_length=1_500)
    scope: Literal["order"] = "order"
    provider_notice: str = Field(serialization_alias="providerNotice")
