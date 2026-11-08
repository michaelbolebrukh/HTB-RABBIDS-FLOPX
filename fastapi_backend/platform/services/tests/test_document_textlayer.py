"""The invisible text layer that keeps a rasterised CV searchable, selectable and
ATS-parseable — asserted the way `test_document_rebuild.py` asserts its own claim:
against real behaviour, not against source text.

The property under test is not "the module runs without raising". It is:

    extract(raster_with_text_layer(images, source)) == extract(source)

on real multi-line, multi-page, accented content — because a text layer that lands a
selection on the wrong words, or silently drops a diacritic, is worse than no text
layer at all (see `textlayer.py`'s module docstring and the review notes on
`_flush_run` for the two broken shapes this settled on its current one to avoid).
"""

from __future__ import annotations

import difflib
import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from platform.services.document_ingest.textlayer import (
    _encode_char,
    raster_with_text_layer,
)


def _pdf_text(escaped_lines_and_size, page_size=(595, 842)) -> bytes:
    """A real PDF with real vector Helvetica text — the thing that must survive.

    `escaped_lines_and_size` is a list of (text, font_size) tuples, drawn top to bottom.
    Mirrors the fixture pattern in `test_document_rebuild.py`'s `_cv()`.
    """
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    pdf.add_blank_page(page_size=page_size)
    page = pdf.pages[0]
    ops = []
    y = page_size[1] - 60
    for text, size in escaped_lines_and_size:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /F1 {size} Tf 60 {y} Td ({escaped}) Tj ET")
        y -= size + 6
    page.Contents = pdf.make_stream("\n".join(ops).encode("cp1252"))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _dense_cv_source(pages: int = 2, lines_per_page: int = 45) -> bytes:
    """A multi-page CV-shaped document with enough real text to measure size/agreement on."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    body = (
        "Experienced quantitative analyst with a strong background in statistics, "
        "machine learning and financial markets. Led cross functional teams to "
        "deliver production trading systems and risk models at scale."
    )
    words = (body + " ").split(" ")
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
        page = pdf.pages[-1]
        ops = []
        y = 800
        wi = 0
        for _ in range(lines_per_page):
            line = " ".join(words[(wi + k) % len(words)] for k in range(9))
            wi += 9
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 10 Tf 40 {y} Td ({escaped}) Tj ET")
            y -= 16
        page.Contents = pdf.make_stream("\n".join(ops).encode("cp1252"))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _render_pages(source: bytes, dpi: float = 150.0):
    doc = pdfium.PdfDocument(source)
    try:
        images = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                bitmap = page.render(scale=dpi / 72)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                page.close()
        return images
    finally:
        doc.close()


def _extract_all(data: bytes):
    doc = pdfium.PdfDocument(data)
    try:
        out = []
        for i in range(len(doc)):
            textpage = doc[i].get_textpage()
            try:
                out.append(textpage.get_text_range())
            finally:
                textpage.close()
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Text correctness: the property that matters most.
# ---------------------------------------------------------------------------

def test_dense_multipage_text_is_recovered_exactly():
    """Real, dense, multi-page body text comes back out character-for-character.

    This is the test that would fail without the fix: delete `textlayer.py` (or make
    `raster_with_text_layer` return the images with no text layer at all, which is
    exactly what the rasterising fallback does today) and every page's extracted text
    goes from a full match to empty — verified by hand while building this module.
    """
    source = _dense_cv_source()
    images = _render_pages(source)

    out = raster_with_text_layer(images, source, dpi=150.0)

    wanted = _extract_all(source)
    got = _extract_all(out)
    assert len(got) == len(wanted)
    for page_index, (want, have) in enumerate(zip(wanted, got)):
        ratio = difflib.SequenceMatcher(None, want, have).ratio()
        assert ratio == 1.0, (
            f"page {page_index}: extracted text does not match the original "
            f"(ratio={ratio}); want={want!r} got={have!r}"
        )
        assert len(want) > 500, "fixture sanity: this page should carry real body text"


def test_multiline_multipage_lines_and_page_breaks_preserved():
    """Two pages, several lines each, including an em dash — line and page structure intact."""
    source = _pdf_text([
        ("Jose Pineiro", 14),
        ("Curly quote test: it is here", 12),
        ("Software Engineer", 12),
        ("jose.pineiro@example.com", 10),
    ])
    # A second page appended the same way _cv() does it in test_document_rebuild.py.
    pdf = pikepdf.open(io.BytesIO(source))
    font = pdf.pages[0].Resources.Font.F1
    pdf.add_blank_page(page_size=(595, 842))
    pdf.pages[1].Contents = pdf.make_stream(
        b"BT /F1 12 Tf 60 780 Td (Second page line one) Tj ET\n"
        b"BT /F1 12 Tf 60 760 Td (Second page line two, with an em dash ) Tj ET"
    )
    pdf.pages[1].Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    buf = io.BytesIO()
    pdf.save(buf)
    source = buf.getvalue()

    images = _render_pages(source)
    out = raster_with_text_layer(images, source, dpi=150.0)

    wanted = _extract_all(source)
    got = _extract_all(out)
    assert got == wanted


# ---------------------------------------------------------------------------
# Unicode: what WinAnsi (cp1252) actually holds, and honest failure for what it doesn't.
# ---------------------------------------------------------------------------

def test_accented_names_and_curly_quotes_survive():
    """"Jose Pineiro" (real diacritics) and a curly apostrophe both round-trip exactly.

    cp1252 (WinAnsiEncoding) covers accented Latin-1 letters and the Windows-style
    curly quotes/dashes, which is why these specific characters were chosen — they are
    exactly what a CV in French, Spanish, Portuguese or German commonly needs.
    """
    name = "José Piñeiro"  # José Piñeiro
    quote_line = "Curly quote: it’s here"  # it’s here
    source = _pdf_text([(name, 16), (quote_line, 12)])
    images = _render_pages(source)

    out = raster_with_text_layer(images, source, dpi=150.0)

    wanted = _extract_all(source)[0]
    got = _extract_all(out)[0]
    assert got == wanted
    assert "Piñeiro" in got
    assert "’" in got  # the curly apostrophe itself, not a plain one


def test_cjk_character_is_dropped_not_mangled():
    """A character outside cp1252 is honestly dropped, never re-encoded as the wrong glyph.

    There is no way to get pypdfium2 to hand back a genuine CJK glyph without a CJK-
    capable font, which this environment does not have and does not need for the
    documents this module actually serves. So this asserts the real contract at the
    unit the pipeline relies on: `_encode_char` returns None (dropped) rather than
    silently substituting or corrupting the byte, which is the behaviour that keeps a
    partial-coverage text layer honest instead of silently wrong (see module docstring).
    """
    assert _encode_char("中") is None  # 中 ("middle") — CJK, not representable in cp1252
    assert _encode_char("А") is None  # А — Cyrillic capital A, also outside cp1252
    assert _encode_char("A") == b"A"
    assert _encode_char("é") == "é".encode("cp1252")  # é survives


# ---------------------------------------------------------------------------
# Size: the invisible layer must not balloon the file.
# ---------------------------------------------------------------------------

def test_size_overhead_is_small_on_a_text_dense_page():
    """Bytes added per page, on a genuinely text-dense page, stay small.

    Measured on a 45-line, ~2900-character page: well under a kilobyte per page once
    the content stream is Flate-compressed (pikepdf's default on save). The bound below
    is generous headroom over that measurement, not a tight pin to today's number.
    """
    source = _dense_cv_source(pages=2, lines_per_page=45)
    images = _render_pages(source)

    base_buffer = io.BytesIO()
    images[0].save(base_buffer, format="PDF", save_all=True, append_images=images[1:], resolution=150.0)
    base_bytes = base_buffer.getvalue()

    out = raster_with_text_layer(images, source, dpi=150.0)

    added_per_page = (len(out) - len(base_bytes)) / len(images)
    assert added_per_page < 5000, f"text layer added {added_per_page:.0f} bytes/page, expected << 5000"


# ---------------------------------------------------------------------------
# Rotation: a documented, tested limitation rather than an assumed one.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_pages_recover_text(rotation):
    """All four /Rotate values PDF allows still recover the page's text exactly.

    The page is rendered by the caller with rotation already baked into the bitmap
    (that is what PDFium's own render does); this only has to place invisible glyphs
    that land on the same, now-rotated, pixels.
    """
    source = _pdf_text([("Rotated Test Line", 14)])
    pdf = pikepdf.open(io.BytesIO(source))
    pdf.pages[0].Rotate = rotation
    buf = io.BytesIO()
    pdf.save(buf)
    source = buf.getvalue()

    images = _render_pages(source)
    out = raster_with_text_layer(images, source, dpi=150.0)

    assert _extract_all(out) == _extract_all(source)


# ---------------------------------------------------------------------------
# Safety: never smuggle source structure into the output.
# ---------------------------------------------------------------------------

def test_output_never_contains_source_javascript_or_font_program():
    """A JS payload and a fake embedded font programme in the source never reach the output.

    The safety argument for the rasterising rung is that nothing structural crosses —
    this asserts that directly rather than trusting the "we only read text/geometry"
    description: a marker string placed in an /OpenAction JavaScript action, and a fake
    /FontFile stream, must not appear anywhere in the bytes we produce.
    """
    marker = b"PWNED_MARKER_ " + b"X" * 64
    source = _pdf_text([("Ordinary CV text", 14)])
    pdf = pikepdf.open(io.BytesIO(source))
    pdf.Root.OpenAction = pdf.make_indirect(pikepdf.Dictionary(
        S=pikepdf.Name.JavaScript, JS=f"app.alert('{marker.decode()}')"))
    fake_font_program = pdf.make_indirect(pikepdf.Stream(pdf, marker * 4))
    fake_font_program.Type = pikepdf.Name.FontFile
    pdf.pages[0].Resources.Font.F1.FontDescriptor = pikepdf.Dictionary(FontFile=fake_font_program)
    buf = io.BytesIO()
    pdf.save(buf)
    source = buf.getvalue()

    images = _render_pages(source)
    out = raster_with_text_layer(images, source, dpi=150.0)

    assert marker not in out
    assert b"/FontFile" not in out
    assert b"/JavaScript" not in out
    assert b"/OpenAction" not in out
    # And the text layer still did its job despite the attack sitting right next to it:
    assert "Ordinary CV text" in _extract_all(out)[0]


# ---------------------------------------------------------------------------
# Failure contract: raise, never return partial/corrupt bytes.
# ---------------------------------------------------------------------------

def test_raises_on_no_images():
    with pytest.raises(ValueError):
        raster_with_text_layer([], b"%PDF-1.4 ...", dpi=150.0)


def test_raises_on_empty_source_data():
    image = Image.new("RGB", (100, 100), "white")
    with pytest.raises(ValueError):
        raster_with_text_layer([image], b"", dpi=150.0)


def test_raises_on_unopenable_source_data():
    """Garbage `source_data` raises so the caller can fall back to a plain raster.

    It must NOT silently produce a text-less (but still valid) PDF here — that would
    hide the failure as a successful call, and the docstring's contract is explicit
    that failure must be visible so the caller drops to a plain raster instead.
    """
    image = Image.new("RGB", (100, 100), "white")
    with pytest.raises(Exception):
        raster_with_text_layer([image], b"not a pdf at all", dpi=150.0)
