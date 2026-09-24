"""Generador de PDF mínimo y sin dependencias: texto, tablas con saltos de página y pie.

Usa las fuentes estándar Helvetica y Helvetica-Bold (WinAnsi), así que no incrusta fuentes: el
archivo es pequeño y cualquier lector lo abre. Cubre lo que necesita un informe de auditoría:
título, pares clave-valor, párrafos y tablas con cabecera repetida en cada página.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
MARGIN = 40.0
BODY_SIZE = 9.0
LEADING = 12.0
_BOLD_FACTOR = 1.08

# Anchos de Helvetica en milésimas de em; lo no listado usa 556 (dígitos y minúsculas anchas).
_WIDTHS: dict[str, int] = {
    **{c: 556 for c in "0123456789abdeghnopquL"},
    " ": 278, "f": 278, "i": 222, "j": 222, "l": 222, "t": 278, "r": 333, "m": 833,
    "c": 500, "k": 500, "s": 500, "v": 500, "x": 500, "y": 500, "z": 500, "w": 722,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "M": 833, "N": 722, "O": 778, "P": 667, "Q": 778,
    "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667,
    "Z": 611, ".": 278, ",": 278, ":": 278, ";": 278, "/": 278, "'": 191, "|": 260,
    "-": 333, "(": 333, ")": 333, "[": 278, "]": 278, "@": 1015, "%": 889, "&": 667,
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    total = sum(_WIDTHS.get(char, 556) for char in text) * size / 1000.0
    return total * (_BOLD_FACTOR if bold else 1.0)


def _clean(text: object) -> str:
    return _CONTROL.sub("", str(text)).replace("\t", " ")


def _escape(text: str) -> str:
    """Cadena literal PDF en cp1252: escapa ``\\ ( )`` y lo no ASCII en octal."""
    out: list[str] = []
    for byte in text.encode("cp1252", errors="replace"):
        char = chr(byte)
        if char in "\\()":
            out.append("\\" + char)
        elif 32 <= byte < 127:
            out.append(char)
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)


def wrap(text: str, width: float, size: float, *, bold: bool = False) -> list[str]:
    """Parte el texto en líneas que caben en ``width``; corta palabras más largas que la línea."""
    lines: list[str] = []
    for paragraph in _clean(text).split("\n"):
        current = ""
        for word in paragraph.split(" "):
            while text_width(word, size, bold=bold) > width and len(word) > 1:
                room = max(1, int(len(word) * width / text_width(word, size, bold=bold)))
