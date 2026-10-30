"""Generador de PDF: estructura válida, texto con acentos, paginación y escapes."""

from __future__ import annotations

import re

from app.core.reports.pdf import PdfReport, text_width, wrap


def _build(rows: int = 3) -> bytes:
    report = PdfReport("Informe de auditoría", "Periodo de prueba", "Rutas Pasto · confidencial")
    report.heading("Resumen")
    report.key_values([("Nombre", "Ana (repartidora) \\ prueba"), ("Estado", "Disponible")])
    report.paragraph("Texto con acentos: ñ, á, é, ü, ¿qué?")
    report.table(
        ["Fecha", "Acción", "Resultado"],
        [[f"2026-11-20 10:{i % 60:02d}", f"accion_{i}", "permitido"] for i in range(rows)],
        [2, 3, 2],
    )
    return report.build()


def test_pdf_has_valid_structure_and_xref_offsets() -> None:
    pdf = _build()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    startxref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert pdf[startxref:startxref + 4] == b"xref"
    table = pdf[startxref:].split(b"\n")
    count = int(table[1].split()[1])
    entries = [line for line in table[2:2 + count] if line.endswith(b"n ")]
    assert len(entries) == count - 1
    for number, line in enumerate(entries, start=1):
        offset = int(line.split()[0])
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


def test_page_count_matches_pages_object_and_grows_with_rows() -> None:
    small = _build(3)
    big = _build(400)
