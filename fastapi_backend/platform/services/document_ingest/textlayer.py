"""Invisible text layer for the rasterising fallback: keep a rasterised CV searchable.

The rasterising rung of `disarm.py` exists because some CVs cannot be safely handed
through the allowlist rebuild, and the product now guarantees no CV is ever refused.
Rasterising alone loses every character of text — no copy-paste, no ATS parsing, no
screen reader. There is no OCR in this environment (no tesseract, no poppler) and none
is being added; none is needed, because the SOURCE PDF already carries its own text and
per-character glyph positions. This module lifts that text back out with pypdfium2 and
paints it, invisibly, over the raster images the caller already rendered.

Safety boundary: the output is built by us from images plus text strings and
coordinates only. We open the source document read-only, purely to ask PDFium for text
and geometry — we never copy an object, a stream, a font programme or a filter chain
out of it. The one PDF structure we DO reopen and edit afterwards (via pikepdf) is the
raster PDF Pillow just built from the caller's own images, which is our own
construction and carries nothing from the untrusted upload. The invisible glyphs use a
standard base-14 font (Helvetica, WinAnsiEncoding) — never a font programme extracted
from the source — because nothing about this layer is ever seen, only selected, so
metric fidelity matters far less than never embedding attacker-controlled font bytes.
"""

from __future__ import annotations

import io
from typing import List, Optional, Tuple

_MIN_FONT_SIZE = 0.75

# WinAnsiEncoding is PDF's name for (almost exactly) Windows code page 1252. It holds
# the Latin-1 accented letters a CV name commonly needs (e.g. "e" with acute, "n" with
# tilde) plus the Windows-style curly quotes and dashes in the 0x80-0x9F block. It does
# NOT hold anything outside that single byte, so Cyrillic, Greek, or CJK characters
# cannot be represented by this font at all. A character that cannot be encoded is
# dropped from the invisible layer rather than substituted with the wrong glyph —
# silently wrong text is worse than a small gap in coverage (see module docstring).
_ENCODING_NAME = "cp1252"


def _rotate_point(x: float, y: float, width: float, height: float, rotation: int) -> Tuple[float, float]:
    """Map a point in the page's own (unrotated) coordinate space into display space.

    `rotation` is PDFium's `get_rotation()`, the page's own clockwise /Rotate value in
    degrees. PDFium bakes that rotation into the bitmap it renders (that render is done
    by the caller, not here), so a character's box has to move through the same
    transform to land on the matching pixels. Restricted to the four values the PDF
    spec allows; anything else is treated as unrotated rather than guessed at.
    """
    rotation = rotation % 360
    if rotation == 90:
        return height - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, width - x
    return x, y


def _rotate_box(
    left: float, bottom: float, right: float, top: float,
    width: float, height: float, rotation: int,
) -> Tuple[float, float, float, float]:
    """Rotate an axis-aligned char box by a multiple of 90 degrees.

    Rotating the two opposite corners and re-deriving min/max keeps the result
    axis-aligned (true for any multiple of 90) and automatically swaps width and
    height for the 90/270 cases, matching the swapped page dimensions of a rotated
    render.
    """
    x0, y0 = _rotate_point(left, bottom, width, height, rotation)
    x1, y1 = _rotate_point(right, top, width, height, rotation)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _char_box(textpage, index: int) -> Optional[Tuple[str, float, float, float, float]]:
    """A single character's text and (left, bottom, right, top), or None to skip it.

    Deliberately the LOOSE box (the font's design ascent/descent bounds), not PDFium's
    default tight ink box. The tight box's top/bottom tracks each glyph's own ink —
    constant for "n"/"o"/"e" but taller for an ascender like "l" and shorter for a
    descender like "g" — so a run of ordinary text produces a different font size for
    almost every character. Measured effect: text extracted back out of a first attempt
    built on tight boxes came back with a spurious line break between nearly every
    character, because the extractor reads big font-size jumps as a new line. The loose
    box holds constant across an entire run at one font/size (measured: identical top
    and bottom for every character in a test line), which is what both gives a stable,
    correct font size AND matches how the source document's own lines are shaped. It is
    also nonzero for whitespace, which the tight box is not — so this same choice is
    what keeps a space from vanishing as a degenerate, skipped character.
    """
    text = textpage.get_text_range(index, 1)
    if not text:
        return None
    left, bottom, right, top = textpage.get_charbox(index, loose=True)
    if right - left <= 0 or top - bottom <= 0:
        return None
    return text, left, bottom, right, top


def _pdf_literal(raw: bytes) -> bytes:
    """Escape bytes for a PDF literal string, i.e. the ( ... ) form used by Tj."""
    out = bytearray()
    for b in raw:
        if b in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
        out.append(b)
    return bytes(out)


_LINE_TOLERANCE = 0.05  # points; chars within this of each other's baseline count as one line


# Text-space basis vectors for each of the four /Rotate values PDF allows, i.e. what
# "one unit rightward in the line's own text space" becomes once mapped through
# _rotate_point into display space. Used as the (a, b, c, d) of the line's Tm so
# invisible glyphs run in the same direction the rotated raster image actually reads in,
# rather than always horizontally regardless of how the page is rotated.
_ROTATION_MATRIX = {
    0: (1.0, 0.0, 0.0, 1.0),
    90: (0.0, 1.0, -1.0, 0.0),
    180: (-1.0, 0.0, 0.0, -1.0),
    270: (0.0, -1.0, 1.0, 0.0),
}


def _flush_run(run: List[Tuple[bytes, float]], left0: float, bottom: float, font_size: float,
               font_resource_name: str, rotation: int) -> bytes:
    """Turn one line's buffered glyph bytes into a Tm + Tj fragment, one string per line.

    Two other shapes were tried and measured against real content before this one:

    1. One absolute Tm reset per character. Positions were exactly right, but re-reading
       the result back out with PDFium inserted a spurious space or line break after
       nearly every glyph — reproducibly, on real-CV-sized (roughly A4) pages, while the
       same content stayed clean on much smaller ones. Whatever heuristic PDFium's
       extractor uses to decide "same run" evidently keys on something in that shape at
       real-world scale.
    2. A single TJ array with one string per glyph and a kerning-style position
       correction between every pair. That fixed the line-break corruption but
       introduced a NEW one: PDFium inserted a space between every string ITEM in the
       array regardless of the numeric correction's size — i.e. it treats each string
       boundary in a TJ array as a word boundary, not just each large gap.

    So: one PLAIN STRING per line, Tm placed once at the line's true start. Standard-14
    fonts carry their own metrics without our needing an explicit /Widths array, so
    Helvetica's built-in advance for each character does the horizontal spacing — this
    trades a little positional drift on a long line (the source's real font is rarely
    Helvetica, so an accumulated width mismatch is possible well into a line) for a
    shape that reads back as ordinary, unbroken text. That trade is the right one here:
    the caller only needs a shown SELECTION to land close enough to be usable and the
    EXTRACTED text to be exactly right — position drift can cost the former a little,
    but only broken run boundaries could cost the latter, and this shape has none.
    """
    text = b"".join(glyph for glyph, _true_left in run)
    a, b, c, d = _ROTATION_MATRIX.get(rotation % 360, _ROTATION_MATRIX[0])
    return b"".join([
        b"%.4f %.4f %.4f %.4f %.2f %.2f Tm\n" % (a, b, c, d, left0, bottom),
        b"/%s %.2f Tf 100 Tz\n" % (font_resource_name.encode("ascii"), font_size),
        b"(" + text + b") Tj\n",
    ])


def _page_operators(source_page, font_resource_name: str) -> Optional[bytes]:
    """The invisible-text content-stream fragment for one page, or None if empty.

    Characters are grouped into per-line runs and drawn with a single Tj string per
    line rather than an absolute Tm reset per character — see `_flush_run` for why.
    `Tf` is re-issued once per line/font-size, not per character.

    Line membership is decided on the ORIGINAL, unrotated baseline (the page's own
    `bottom` coordinate before `_rotate_box`), not the rotated one: rotation maps a
    page's horizontal lines onto what is, after transform, a vertical run of differing
    "bottom" values in display space, so grouping on the rotated coordinate would put
    every character of a 90-or-270-rotated page into its own single-character run —
    the same corruption a per-character Tm reset caused (see `_flush_run`), just
    reached from the rotation angle instead of from the grouping itself.
    """
    textpage = source_page.get_textpage()
    try:
        width, height = source_page.get_size()
        rotation = source_page.get_rotation()
        count = textpage.count_chars()
        lines: List[bytes] = []
        run: List[Tuple[bytes, float]] = []
        run_source_bottom: Optional[float] = None
        run_bottom: Optional[float] = None
        run_font_size: Optional[float] = None
        run_left0: Optional[float] = None

        def flush():
            if run:
                lines.append(
                    _flush_run(run, run_left0, run_bottom, run_font_size, font_resource_name, rotation)
                )

        for index in range(count):
            got = _char_box(textpage, index)
            if got is None:
                continue
            text, s_left, s_bottom, s_right, s_top = got
            # Font size is a text-space property (how tall the glyph is drawn), not a
            # display one — it must come from the box BEFORE rotation. The rotated box's
            # own "top minus bottom" is a different physical quantity once axes swap
            # under a 90/270 rotation (it becomes the character's WIDTH, which varies
            # letter to letter), so using it here would both pick a wrong font size and
            # — fed back into the same-line check — break line grouping exactly the way
            # ungrouped per-character runs already proved corrupts extraction.
            font_size = max(s_top - s_bottom, _MIN_FONT_SIZE)
            left, bottom, right, top = _rotate_box(s_left, s_bottom, s_right, s_top, width, height, rotation)

            encoded = _encode_char(text)
            if encoded is None:
                continue

            same_line = (
                run_source_bottom is not None
                and abs(s_bottom - run_source_bottom) <= _LINE_TOLERANCE
                and abs(font_size - run_font_size) <= _LINE_TOLERANCE
            )
            if not same_line:
                flush()
                run = []
                run_source_bottom = s_bottom
                run_bottom = bottom
                run_font_size = font_size
                run_left0 = left
            run.append((_pdf_literal(encoded), left))
        flush()

        if not lines:
            return None
        return b"BT\n3 Tr\n" + b"".join(lines) + b"ET\n"
    finally:
        textpage.close()


def _encode_char(text: str) -> Optional[bytes]:
    """A character (or the rare multi-codepoint cluster PDFium hands back) as WinAnsi bytes.

    Returns None the moment any part of it falls outside cp1252 — a curly apostrophe or
    an accented Latin name survives whole; a CJK character is dropped whole rather than
    emitting a mangled partial encoding of it.
    """
    try:
        return text.encode(_ENCODING_NAME)
    except UnicodeEncodeError:
        return None


def raster_with_text_layer(page_images, source_data: bytes, *, dpi: float) -> bytes:
    """Build a raster PDF from `page_images` with an invisible, selectable text layer.

    `page_images` are PIL RGB images already rendered by the caller at `dpi`, in page
    order — this function never renders anything itself, it only reads `source_data`
    (the original upload) to recover the text PDFium can already see in it, and paints
    that text back over the images at matching coordinates.

    Raises on any failure (empty input, an unopenable source, PDFium/pikepdf errors) so
    the caller can fall back to a plain raster with no text layer at all. Never returns
    partial or corrupt PDF bytes.
    """
    if not page_images:
        raise ValueError("raster_with_text_layer requires at least one page image")
    if not source_data:
        raise ValueError("raster_with_text_layer requires the original document bytes")

    import pikepdf
    import pypdfium2 as pdfium

    # Step 1: the base document is built ENTIRELY from the caller's own images. This is
    # our own construction, not a parse of the untrusted upload, which is what makes it
    # safe to reopen and edit in step 2.
    buffer = io.BytesIO()
    page_images[0].save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=list(page_images[1:]),
        resolution=dpi,
    )
    buffer.seek(0)

    out = pikepdf.open(buffer)
    try:
        font = out.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        ))

        source = pdfium.PdfDocument(source_data)
        try:
            page_count = min(len(source), len(out.pages))
            for index in range(page_count):
                source_page = source[index]
                try:
                    out_page = out.pages[index]
                    resource_name = str(out_page.add_resource(font, pikepdf.Name.Font))
                    operators = _page_operators(source_page, resource_name.lstrip("/"))
                    if operators:
                        out_page.contents_add(pikepdf.Stream(out, operators), prepend=False)
                finally:
                    source_page.close()
        finally:
            source.close()

        result = io.BytesIO()
        out.save(result)
        return result.getvalue()
    finally:
        out.close()
