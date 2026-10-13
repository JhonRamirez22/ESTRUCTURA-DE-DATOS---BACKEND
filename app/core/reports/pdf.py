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
                head, word = word[:room], word[room:]
                if current:
                    lines.append(current)
                    current = ""
                lines.append(head)
            candidate = word if not current else f"{current} {word}"
            if current and text_width(candidate, size, bold=bold) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


class _Page:
    def __init__(self) -> None:
        self.ops: list[str] = []


class PdfReport:
    def __init__(self, title: str, subtitle: str, footer: str) -> None:
        self._title = _clean(title)
        self._footer = _clean(footer)
        self._pages: list[_Page] = []
        self._y = 0.0
        self._new_page()
        self._text(MARGIN, self._y - 14, self._title, 16, bold=True)
        self._y -= 30
        for line in wrap(subtitle, PAGE_WIDTH - 2 * MARGIN, 9):
            self._text(MARGIN, self._y - 9, line, 9, gray=0.35)
            self._y -= LEADING
        self._rule()
        self._y -= 8

    # ---- contenido -------------------------------------------------------------------

    def heading(self, text: str) -> None:
        self._ensure(40)
        self._y -= 8
        self._text(MARGIN, self._y - 11, _clean(text), 11.5, bold=True)
        self._y -= 18

    def paragraph(self, text: str, *, gray: float = 0.0) -> None:
        for line in wrap(text, PAGE_WIDTH - 2 * MARGIN, BODY_SIZE):
            self._ensure(LEADING)
            self._text(MARGIN, self._y - 9, line, BODY_SIZE, gray=gray)
            self._y -= LEADING
        self._y -= 4

    def key_values(self, pairs: Sequence[tuple[str, object]]) -> None:
        label_w = 150.0
        value_w = PAGE_WIDTH - 2 * MARGIN - label_w
        for label, value in pairs:
            lines = wrap(str(value), value_w, BODY_SIZE)
            self._ensure(LEADING * len(lines))
            self._text(MARGIN, self._y - 9, _clean(label), BODY_SIZE, bold=True, gray=0.25)
            for line in lines:
                self._text(MARGIN + label_w, self._y - 9, line, BODY_SIZE)
                self._y -= LEADING
        self._y -= 4

    def table(
        self, headers: Sequence[str], rows: Sequence[Sequence[object]], fractions: Sequence[float]
    ) -> None:
        total = sum(fractions)
        widths = [(PAGE_WIDTH - 2 * MARGIN) * f / total for f in fractions]
        if not rows:
            self.paragraph("Sin registros en el periodo.", gray=0.4)
            return
        self._table_header(headers, widths)
        for index, row in enumerate(rows):
            cells = [
                wrap(str(value), width - 6, BODY_SIZE - 0.5)
                for value, width in zip(row, widths, strict=False)
            ]
            height = max(len(lines) for lines in cells) * (LEADING - 1) + 5
            if self._y - height < MARGIN + 24:
                self._new_page()
                self._table_header(headers, widths)
            if index % 2 == 1:
                self._rect(MARGIN, self._y - height, sum(widths), height, 0.96)
            x = MARGIN
            for lines, width in zip(cells, widths, strict=False):
                for offset, line in enumerate(lines):
                    self._text(
                        x + 3, self._y - 9 - offset * (LEADING - 1), line, BODY_SIZE - 0.5
                    )
                x += width
            self._y -= height
        self._y -= 6

    # ---- salida ----------------------------------------------------------------------

    def build(self) -> bytes:
        total = len(self._pages)
        for number, page in enumerate(self._pages, start=1):
            page.ops.append("0.6 0.6 0.6 RG 0.5 w "
                            f"{MARGIN} {MARGIN - 6} m {PAGE_WIDTH - MARGIN} {MARGIN - 6} l S")
            page.ops.append(self._text_op(MARGIN, MARGIN - 18, self._footer, 7.5, False, 0.4))
            label = f"Página {number} de {total}"
            width = text_width(label, 7.5)
            page.ops.append(
                self._text_op(PAGE_WIDTH - MARGIN - width, MARGIN - 18, label, 7.5, False, 0.4)
            )
        objects: list[bytes] = []
        page_ids = [5 + 2 * i for i in range(total)]
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{i} 0 R" for i in page_ids)
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {total} >>".encode())
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        for number, page in enumerate(self._pages):
            content = "\n".join(page.ops).encode("ascii")
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH:g} {PAGE_HEIGHT:g}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                    f"/Contents {page_ids[number] + 1} 0 R >>"
                ).encode()
            )
            objects.append(
                b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
