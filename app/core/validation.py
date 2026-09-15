"""Validaciones de entrada puras: correo estricto y texto sin caracteres de control."""

from __future__ import annotations

import re

MAX_EMAIL_LENGTH = 254
_LOCAL_PART = re.compile(r"[a-z0-9]+(?:[._%+-][a-z0-9]+)*")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"[a-z]{2,24}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_email(value: str) -> str:
    """Exige usuario@dominio.tld en ASCII; rechaza espacios, control, puntos dobles y TLD cortos."""
    email = value.strip().casefold()
    if not email.isascii() or len(email) > MAX_EMAIL_LENGTH:
        raise ValueError("Ingresa un correo válido, por ejemplo nombre@dominio.com.")
    local, separator, domain = email.partition("@")
    labels = domain.split(".")
    valid = (
        separator == "@"
        and 0 < len(local) <= 64
        and _LOCAL_PART.fullmatch(local) is not None
        and len(labels) >= 2
        and all(_DOMAIN_LABEL.fullmatch(label) for label in labels[:-1])
        and _TLD.fullmatch(labels[-1]) is not None
    )
    if not valid:
        raise ValueError("Ingresa un correo válido, por ejemplo nombre@dominio.com.")
    return email


def plain_text(value: str, *, max_length: int, field: str) -> str:
    """Texto de una sola línea: sin caracteres de control y con longitud acotada."""
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{field} no puede superar {max_length} caracteres.")
    if _CONTROL_CHARACTERS.search(text):
        raise ValueError(f"{field} contiene caracteres no permitidos.")
    return text
