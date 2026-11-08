"""Fail-closed safe PDF ingest — ephemeral OG, durable SAFE only."""

import io
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from platform.exceptions import DocumentDisarmFailedException, InvalidDocumentException
from platform.services.document_ingest import disarm_pdf_to_safe_bytes, ingest_private_safe_pdf
from platform.services.storage.local import LocalObjectStorage


def _simple_pdf(*, pages: int = 1, marker: bytes = b"") -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(200, 200))
    if marker:
        # Stuff a unique byte string into an OpenAction JS payload so we can
        # prove the stored artefact is not the original:
        js = pdf.make_indirect(
            pikepdf.Dictionary(
                S=pikepdf.Name.JavaScript,
                JS=marker.decode("latin-1", errors="ignore") or "x",
            )
        )
        pdf.Root.OpenAction = js
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_disarm_returns_pdf_not_equal_to_original():
    marker = b"UNIQUE_OG_MARKER_7f3a"
    raw = _simple_pdf(marker=marker)
    safe = disarm_pdf_to_safe_bytes(raw)
    assert safe.startswith(b"%PDF-")
    assert safe != raw
    assert marker not in safe


def test_disarm_rejects_non_pdf():
    with pytest.raises(InvalidDocumentException):
        disarm_pdf_to_safe_bytes(b"not-a-pdf")


def test_disarm_rejects_empty():
    with pytest.raises(InvalidDocumentException):
        disarm_pdf_to_safe_bytes(b"")


def test_disarm_rejects_encrypted_pdf():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    buffer = io.BytesIO()
    pdf.save(buffer, encryption=pikepdf.Encryption(owner="owner", user="user", R=4))
    with pytest.raises(DocumentDisarmFailedException):
        disarm_pdf_to_safe_bytes(buffer.getvalue(), flatten=True)


def test_disarm_custom_rebuild_seam_never_echoes_input():
    raw = _simple_pdf()

    def _echo(data: bytes) -> bytes:
        return data

    with pytest.raises(DocumentDisarmFailedException):
        disarm_pdf_to_safe_bytes(raw, rebuild=_echo)


def test_ingest_writes_only_safe_key(tmp_path: Path):
    storage = LocalObjectStorage(base_dir=str(tmp_path), public_origin="http://test.local")
    # Local backend is durable when used as the explicit storage arg — we pass
    # it directly so require_durable_storage is not consulted:
    storage.is_durable = True  # type: ignore[misc]

    raw = _simple_pdf(marker=b"OG_MUST_NOT_LAND")
    ingested = ingest_private_safe_pdf(
        raw,
        key_prefix="users/1/cv",
        storage=storage,
    )
    assert "/safe/" in ingested.storage_key
    assert ingested.content_type == "application/pdf"
    assert ingested.page_count >= 1
    stored = storage.read_private(ingested.storage_key)
    assert stored == storage.read_private(ingested.storage_key)
    assert b"OG_MUST_NOT_LAND" not in stored
    # No other private objects (no OG key beside the safe one):
    private_root = Path(storage.private_dir)
    pdfs = list(private_root.rglob("*.pdf"))
    assert len(pdfs) == 1
    assert "safe" in str(pdfs[0])


def test_ingest_rebuild_failure_writes_nothing(tmp_path: Path):
    storage = LocalObjectStorage(base_dir=str(tmp_path), public_origin="http://test.local")
    storage.is_durable = True  # type: ignore[misc]

    def _boom(data: bytes) -> bytes:
        raise RuntimeError("nope")

    with pytest.raises(DocumentDisarmFailedException):
        ingest_private_safe_pdf(
            _simple_pdf(),
            key_prefix="users/1/cv",
            storage=storage,
            rebuild=_boom,
        )
    private_root = Path(storage.private_dir)
    assert list(private_root.rglob("*")) == [] or not any(private_root.rglob("*.pdf"))


# --- resource bombs ---------------------------------------------------------
#
# A PDF declares its own page geometry, so the page size is attacker-controlled
# while the file stays tiny. Rendering at a fixed scale therefore converts a
# ~258-byte upload into a multi-gigabyte bitmap and OOM-kills the worker. These
# pin the two bounds that stop it; the arithmetic is asserted directly because
# reproducing the unbounded case end to end is exactly the allocation under test.


def test_render_scale_is_clamped_for_an_oversized_page():
    from platform.services.document_ingest.disarm import (
        _MAX_TOTAL_PIXELS,
        _RENDER_MAX_PX,
        _RENDER_SCALE,
        _clamped_scale,
    )

    # The documented bomb: a MediaBox of 14400x14400 (200in square, the PDF
    # spec's maximum page). At the unclamped 150-DPI scale this renders to
    # 30000x30000 px = 3.6GB for ONE page:
    bomb = _clamped_scale(14400, 14400)
    assert bomb * 14400 <= _RENDER_MAX_PX
    assert bomb < _RENDER_SCALE  # it really was clamped, not merely passed through
    # And the bounded bitmap is small enough to be survivable:
    assert (bomb * 14400) * (bomb * 14400) < _MAX_TOTAL_PIXELS


def test_render_scale_is_untouched_for_an_ordinary_page():
    """The clamp must not quietly degrade real CVs — A4 at 150 DPI is ~1754px
    on its longest side, well inside the ceiling, so it keeps the full scale."""
    from platform.services.document_ingest.disarm import _RENDER_SCALE, _clamped_scale

    a4_points = (595.28, 841.89)
    assert _clamped_scale(*a4_points) == _RENDER_SCALE


def test_a_zero_sized_page_yields_no_scale():
    from platform.services.document_ingest.disarm import _clamped_scale

    assert _clamped_scale(0, 0) == 0.0


def test_an_oversized_page_still_disarms_and_stays_bounded():
    """End to end: the bomb geometry is accepted (it is a legal PDF) but the
    stored artefact is a bounded raster, not a 3.6GB allocation."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(14400, 14400))
    buffer = io.BytesIO()
    pdf.save(buffer)

    # flatten=True: these bounds belong to the RASTER path, which is no longer
    # the default. They still guard it, so they are still asserted — against
    # the path that has them rather than through a default that does not.
    safe = disarm_pdf_to_safe_bytes(buffer.getvalue(), flatten=True)

    assert safe.startswith(b"%PDF-")
    with pikepdf.open(io.BytesIO(safe)) as rebuilt:
        page = rebuilt.pages[0]
        width = float(page.MediaBox[2]) - float(page.MediaBox[0])
        height = float(page.MediaBox[3]) - float(page.MediaBox[1])
    # Saved at 150 DPI, so points -> pixels is x150/72; both sides must be
    # within the per-page ceiling:
    from platform.services.document_ingest.disarm import _RENDER_MAX_PX

    assert max(width, height) * (150.0 / 72.0) <= _RENDER_MAX_PX + 1


def test_many_oversized_pages_degrade_to_a_partial_document_rather_than_refuse():
    """The per-page clamp still admits many bomb-sized pages, and every page is
    held in memory until the final save — so the document-wide pixel budget is
    what used to refuse 40 clamped bombs outright. Per the "never refuse for a
    reason short of an unreadable PDF" product requirement, that budget now
    STOPS rendering further pages instead: the upload succeeds with fewer
    pages than it declared, never with none.

    This is a real regression guard, not a decoration: reverting the ladder's
    degrade-not-refuse change on the total-pixel budget makes this fail with
    DocumentDisarmFailedException, exactly as the old (deleted) assertion
    checked for.
    """
    pdf = pikepdf.new()
    for _ in range(40):
        pdf.add_blank_page(page_size=(14400, 14400))
    buffer = io.BytesIO()
    pdf.save(buffer)

    safe = disarm_pdf_to_safe_bytes(buffer.getvalue(), flatten=True)

    assert safe.startswith(b"%PDF-")
    with pikepdf.open(io.BytesIO(safe)) as rebuilt:
        # Degraded, not lost: some pages made it through, but not all 40 —
        # the pixel budget really did stop the render partway rather than
        # silently being bypassed.
        assert 0 < len(rebuilt.pages) < 40


def test_a_page_with_no_usable_size_still_produces_a_document(monkeypatch):
    """A degenerate declared page size (previously "no usable page size",
    a hard refusal) now degrades to a blank stand-in page rather than
    failing the whole document — preserving page count and order over an
    unrenderable page's actual content, which was already lost upstream.

    PDFium itself silently substitutes US-Letter for a genuinely zero-area
    MediaBox (measured directly against pdfium), so the only way to exercise
    the fallback honestly is to force `get_size()` to report the degenerate
    value PDFium never actually surfaces — a NaN, which `_clamped_scale`
    already treats as "no usable scale" for the same reason a zero would be.

    Reverting the fallback-blank-page behaviour in `_render_pages` makes
    this raise DocumentDisarmFailedException instead of succeeding.
    """

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 400))
    pdf.add_blank_page(page_size=(400, 400))
    buffer = io.BytesIO()
    pdf.save(buffer)

    real_get_size = pdfium.PdfPage.get_size
    call_count = {"n": 0}

    def _fake_get_size(self):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return float("nan"), float("nan")
        return real_get_size(self)

    monkeypatch.setattr(pdfium.PdfPage, "get_size", _fake_get_size)

    safe = disarm_pdf_to_safe_bytes(buffer.getvalue(), flatten=True)

    assert safe.startswith(b"%PDF-")
    with pikepdf.open(io.BytesIO(safe)) as rebuilt:
        assert len(rebuilt.pages) == 2


def test_ladder_falls_through_to_raster_when_allowlist_rebuild_cannot_cope():
    """A page using LZW compression is refused outright by the allowlist
    rebuild (`rebuild.py` decodes Flate/ASCII/RunLength only). Under the
    ladder, that must degrade to a rasterised document rather than surface
    as a failed upload — this is the product requirement itself, not an
    implementation detail, so the test goes through the public
    `disarm_pdf_to_safe_bytes` entry point with no rung forced.

    Reverting the ladder (making rung-1 failure raise instead of falling
    through) makes this raise DocumentDisarmFailedException.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    content = pdf.make_stream(b"BT ET", Filter=pikepdf.Name("/LZWDecode"))
    page.obj.Contents = content
    buffer = io.BytesIO()
    pdf.save(buffer)

    safe = disarm_pdf_to_safe_bytes(buffer.getvalue())

    assert safe.startswith(b"%PDF-")
    with pikepdf.open(io.BytesIO(safe)) as rebuilt:
        assert len(rebuilt.pages) == 1


def test_ladder_refuses_only_when_the_document_cannot_be_opened_at_all():
    """The bottom of the ladder — the one remaining refusal short of the
    empty/size/magic checks — is reserved for a document none of the three
    rungs can even open. A truncated file with a valid header but no real
    body is such a case for both pikepdf and PDFium.

    Reverting the ladder to raise eagerly on rung-1 failure would also make
    this pass for the wrong reason, which is why the LZW test above exists
    alongside it: together they pin that rung-1 failure alone must NOT
    refuse, while total unreadability still must.
    """
    with pytest.raises(DocumentDisarmFailedException):
        disarm_pdf_to_safe_bytes(b"%PDF-1.4\n%truncated, not a real document")


def _predictor_bomb(glyphs: int) -> bytes:
    """A small upload whose ONE page draws `glyphs` characters.

    The `/Predictor` matters and is not decoration. It is what makes the
    allowlist rebuild decline this stream as a COMPATIBILITY problem — a
    transform it will not run — while knowing nothing at all about the
    stream's size. That is the gap: a compatibility refusal used to carry no
    cost information, so the ladder rasterised it, and PDFium then executed
    every one of those glyph operations inside a single uninterruptible call
    that the between-pages wall-clock check cannot reach.
    """
    import zlib

    body = b"BT /F1 12 Tf 20 700 Td (" + (b"A" * glyphs) + b") Tj ET"
    compressed = zlib.compress(body, 9)
    return (
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 595 842] >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(compressed)).encode() + b" /Filter /FlateDecode "
        b"/DecodeParms << /Predictor 12 /Colors 1 /BitsPerComponent 8 /Columns 1 >> >> stream\n"
        + compressed + b"\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Root 1 0 R /Size 6 >>\n"
    )


def test_a_content_bomb_is_refused_before_any_renderer_runs():
    """The ladder must establish COST independently of rebuildability.

    Measured before this guard existed: a 390KB upload of exactly this shape
    cost 11.5 seconds and 988MB of RSS, and at 973KB it cost 40 seconds and
    2.1GB — past the gunicorn worker timeout of 30 and past the whole 2GB
    instance, with two such uploads permitted to run concurrently. The upload
    cap cannot catch it, because the file is small; the whole cost is in what
    the content stream expands to.

    Timed as well as asserted, because "refused" alone would still pass if the
    refusal arrived after forty seconds of rendering, which is the actual
    failure this is about.
    """
    import time

    # Above the 64MB content ceiling, which is where the cost probe engages.
    # Measured at that ceiling: 1.8 seconds and +145MB, so the boundary is set
    # from what the renderer actually costs rather than from taste.
    bomb = _predictor_bomb(80_000_000)
    assert len(bomb) < 1024 * 1024, "the bomb must be small on the wire, or it proves nothing"

    started = time.monotonic()
    with pytest.raises(DocumentDisarmFailedException):
        disarm_pdf_to_safe_bytes(bomb)
    assert time.monotonic() - started < 5.0, "refused, but only after paying the cost it exists to avoid"


def test_an_ordinary_predictor_document_still_rasterises_rather_than_being_refused():
    """The other side of that guard, and the one that keeps it honest.

    A `/Predictor` is a perfectly ordinary thing for a generator to emit. The
    rebuild still declines to run one, so this document belongs on the raster
    rung — not in the refusal bucket. A cost probe that cannot tell a small
    predictor document from a bomb would have turned a real defect into a
    worse one.
    """
    safe = disarm_pdf_to_safe_bytes(_predictor_bomb(200))

    assert safe.startswith(b"%PDF-")
    assert len(pdfium.PdfDocument(io.BytesIO(safe))) == 1
