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
    for pdf in (small, big):
        pages = len(re.findall(rb"/Type /Page /Parent", pdf))
        assert int(re.search(rb"/Count (\d+)", pdf).group(1)) == pages  # type: ignore[union-attr]
    assert len(re.findall(rb"/Type /Page /Parent", small)) == 1
    assert len(re.findall(rb"/Type /Page /Parent", big)) > 5
    assert b"P\\341gina 2 de" in big  # «Página» en cp1252 (á = 0o341)


def test_special_characters_are_escaped_and_accents_use_cp1252() -> None:
    pdf = _build()
    assert b"Ana \\(repartidora\\) \\\\ prueba" in pdf
    assert b"\\361" in pdf and b"\\341" in pdf  # ñ y á


def test_table_header_repeats_on_each_page() -> None:
    pdf = _build(400)
    assert pdf.count(b"(Fecha) Tj") == len(re.findall(rb"/Type /Page /Parent", pdf))


def test_wrapping_never_exceeds_width_and_splits_long_words() -> None:
    long_word = "x" * 200
    lines = wrap(f"corto {long_word} fin", 100, 9)
    assert len(lines) > 3
    assert all(text_width(line, 9) <= 100 * 1.15 for line in lines)
    assert wrap("", 100, 9) == [""]
    assert wrap("a\nb", 100, 9) == ["a", "b"]


def test_control_characters_do_not_break_the_file() -> None:
    report = PdfReport("T\x00itulo", "sub\x07", "pie")
    report.paragraph("línea\x00 con\tcontrol\x1b")
    pdf = report.build()
    assert b"\x00(" not in pdf and pdf.rstrip().endswith(b"%%EOF")


def test_empty_table_says_so() -> None:
    report = PdfReport("T", "s", "f")
    report.table(["a", "b"], [], [1, 1])
    assert b"Sin registros" in report.build()
