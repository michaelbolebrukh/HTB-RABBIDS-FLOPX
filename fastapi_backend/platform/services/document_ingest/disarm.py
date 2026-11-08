"""Fail-closed rebuild: as-received PDF -> safe PDF bytes.

`disarm_pdf_to_safe_bytes` runs a LADDER of rebuild strategies, in order of how
much of the document they preserve, and returns the output of the first rung
that succeeds:

    1. Allowlist rebuild (`rebuild.py`) - keeps the text layer, smallest output.
    2. Text-layered raster - every page rasterised, then the ORIGINAL's text
       re-embedded over it as an invisible layer (`textlayer.py`, owned
       elsewhere in this package). Loses vector fidelity but keeps the CV
       searchable, selectable and ATS-parseable.
    3. Plain raster - the same rasterisation with no text layer at all.
    4. Refuse - reserved for a document this module genuinely cannot read:
       empty body, over `MAX_DOCUMENT_BYTES`, missing the `%PDF-` magic, or
       PDFium cannot open it at all.

The ladder exists because of a hard product constraint: nobody re-exports their
CV to satisfy a parser, so a rebuild failure on rungs 1-3 must never surface as
"upload rejected" - it must fall through to a rung that is more tolerant, and
only the bottom of the ladder is allowed to say no. Every rung that is skipped
or fails logs a WARNING naming the rung and the reason, so a member silently
losing their text layer or their vector fidelity is visible in production logs
even though it is invisible to the member themselves.

Original bytes stay in process memory / local temp only. They are never
returned and never written to object storage by this module. The ladder
NEVER falls back to returning the original bytes at any rung - that invariant
is the entire reason this module exists, independent of which rung produced
the output.
"""

from __future__ import annotations

import io
import logging
import math
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import anyio
from PIL import Image

from platform.services.document_ingest.rebuild import rebuild_pdf_preserving_text


logger = logging.getLogger(__name__)


def _resolve_max_bytes(max_bytes: Optional[int]) -> int:
    """The document size cap, taken from the caller or from settings.

    An explicit value is what lets this module run somewhere `settings` cannot
    be constructed. `sandbox.py` executes the ladder in a child process with a
    scrubbed environment — no DATABASE_URL, no HASH_KEY, no credentials of any
    kind — and `infrastructure.settings` raises on import there, by design.
    Passing the integer in is the whole of what the parser needs from config,
    so the isolated process needs nothing else.

    Falling back to settings keeps every in-process caller and every existing
    test working unchanged, and keeps ONE configured value rather than a
    duplicate default that could drift from it.
    """
    if max_bytes is not None:
        return max_bytes
    from infrastructure.settings import settings as s

    return int(s.MAX_DOCUMENT_BYTES)

# Render scale for the TOP of the raster DPI ladder below (PDF user space is
# 72 DPI). 150 DPI is the legibility floor this was chosen against: 9pt body
# text (the smallest a CV realistically uses) renders at 150 DPI to ~18.75px
# tall, comfortably readable on screen. It is also the scale every existing
# render-cost comment in this module was measured against, so the geometry
# bounds below (_RENDER_MAX_PX, _MAX_TOTAL_PIXELS) still describe this scale
# correctly.
_RENDER_SCALE = 150.0 / 72.0

# The raster rungs (2 and 3) try DPIs in this order, stopping at the first one
# whose assembled output fits under MAX_DOCUMENT_BYTES. Dropping DPI shrinks
# BOTH the per-page pixel cost (so more pages survive the total-pixel budget
# below) and the final file size, which is exactly the lever a page-count- or
# density-driven oversize document needs. 150 is the legibility target; 110
# and 85 are visibly softer but still readable, and only get used at all for a
# document that would otherwise be refused outright - a real CV essentially
# never reaches past the first entry.
_RASTER_DPI_LADDER = (150.0, 110.0, 85.0)

# JPEG quality for the raster rungs' page images, and the reason this module
# now encodes pages as JPEG instead of taking Pillow's PDF-save default
# (Flate, i.e. PNG-style lossless compression of the raw pixel grid). Storage
# is a hard product constraint here (10GB free tier holding hundreds of CVs),
# and Flate is a poor fit for a photographically-noisy, anti-aliased page:
# measured on a synthetic 150 DPI A4 page carrying a dense block of 9pt body
# text (worst case - real CVs are far less text-dense per page), Flate came to
# 719KB/page while JPEG at quality 60 came to 585KB/page, about 19% smaller;
# with a simulated headshot photo added to the page (common on CVs outside
# the US/UK), Flate rose to 634KB while JPEG at quality 55 fell to 488KB, a
# 23% reduction. Quality 60 is chosen as the point where the text edges in
# that same measurement stayed crisp under visual inspection while the file
# was still meaningfully smaller than Flate; going materially lower begins to
# fuzz small glyph strokes in a way that would hurt legibility of exactly the
# body text this whole ladder exists to preserve.
_JPEG_QUALITY = 60

# Hard sanity ceiling on page COUNT, kept only to bound the cost of the loop
# itself (get_size + scale-clamp arithmetic per page) against a maliciously
# enormous page count, independent of whether any page ever gets rendered -
# the wall-clock budget below is checked once per page, so a many-thousand
# page document could otherwise spend a long time in that per-page bookkeeping
# before wall-clock ever trips. 500 is far past any real CV (the allowlist
# rebuild ahead of this in the ladder already handles a 60-page CV without
# ever reaching this file), so raising it from the old value of 40 costs
# nothing in practice and buys exactly the behaviour the product owner asked
# for: a 60-page CV renders 60 pages instead of being refused for exceeding a
# limit that had nothing to do with what the worker could actually afford.
_MAX_PAGES = 500

_MAX_WALL_SECONDS = 25.0

# The ceiling on a single rendered page's longest side. A page's dimensions come
# from the FILE, so they are attacker-controlled, and a FIXED scale turns a tiny
# upload into gigabytes of bitmap: a 258-byte PDF declaring a MediaBox of
# 14400x14400 renders at 150 DPI to a 30000x30000 bitmap - 3.6GB for one page,
# which OOM-kills the worker. The upload size limit cannot catch it, because the
# file is tiny; the entire cost is in the declared geometry. The wall-clock guard
# cannot catch it either, because it is only checked BETWEEN pages and this is a
# single allocation. So the scale is clamped to whatever keeps the longest side
# within this bound (`document_identity` clamps its own render the same way).
# This stays a HARD ceiling - not a "render less, still succeed" degrade - because
# there is nothing to degrade to mid-allocation: the allocation either respects the
# bound or it has already happened.
#
# 2200px is chosen to leave the page sizes a CV actually uses untouched: the
# clamp only engages above 1056pt (14.67in) on the longest side, so A4
# (842pt -> 1754px), US Letter and US Legal all keep exactly the fidelity they
# had. A3 and Tabloid DO get downscaled, by roughly 11-14% linear - accepted,
# because a CV is not laid out on A3 and the alternative is leaving the bound
# high enough to matter.
_RENDER_MAX_PX = 2200

# ...and the ceiling on the whole document, because the per-page clamp still
# admits _MAX_PAGES pages of it. Every page is held as a PIL image until the
# final save, so peak memory tracks the SUM, not the largest page. This is the
# other HARD ceiling in this module: it is what stands between a many-page
# document and OOMing the worker, and unlike the per-page clamp it CAN degrade
# gracefully, because there is a meaningful "produce less" available - once
# adding another page's clamped pixels would cross this budget, rendering
# simply stops and everything rendered so far becomes the document. A member
# gets a truncated CV rather than no CV, and the truncation is logged.
#
# Sized against MEASURED peak RSS, not against a pixels x 3 (RGB) model: the
# model understates real peak by ~35%, because PDFium's own render structures,
# the transient BGRA->RGB copy and the Pillow output buffer all coexist. On this
# checkout a 40-page A4 document (87.1M px) peaks at ~361MB, i.e. roughly 4.2MB
# per megapixel. 95M px therefore costs ~400MB, which leaves that legitimate
# 40-page CV working with headroom to spare while bounding the worst case:
_MAX_TOTAL_PIXELS = 95_000_000

# Rebuilds run in a worker thread so they do not block the event loop, and that
# is exactly why they need their own concurrency bound. Before the offload, the
# loop serialised them and the memory ceiling was one rebuild; afterwards the
# default 40-slot threadpool would happily run 40 at once, so the per-request
# ceiling above multiplies. A request-rate limit does NOT bound this - five
# requests inside one "5/minute" window are five simultaneous rebuilds.
#
# Two slots: ~800MB worst case against the 2GB production instance, which keeps
# a headroom margin over the app's own footprint. Uploads beyond that queue for
# a slot rather than being refused, and the loop stays responsive throughout -
# queuing is invisible to every other request, an OOM is not.
_MAX_CONCURRENT_REBUILDS = 2
rebuild_limiter = anyio.CapacityLimiter(_MAX_CONCURRENT_REBUILDS)

# How far into the file the `%PDF-` header may sit before this stops being a
# PDF. See the check itself in `disarm_pdf_to_safe_bytes` for why this is a
# window rather than a prefix comparison.
_PDF_HEADER_WINDOW = 1024


def _clamped_scale(width: float, height: float, base_scale: float = _RENDER_SCALE) -> float:
    """The render scale for a page of this size, never above _RENDER_MAX_PX.

    `base_scale` is the DPI-derived scale the raster rung is currently trying
    (see `_RASTER_DPI_LADDER`); it defaults to the top-of-ladder 150 DPI scale
    so existing callers and tests that only ever cared about that one rung do
    not need to change.

    Split out from the render loop so the bound can be asserted directly: the
    failure this guards against costs gigabytes to reproduce end to end, which
    is precisely why it wants a test that does not have to.
    """
    # Reject non-finite dimensions BEFORE comparing them. A NaN loses every
    # comparison it takes part in, so it would slip past a `<= 0` guard, return
    # the unclamped scale from `min()`, and then poison the caller's running
    # pixel total - where `nan > budget` is also False, silently disabling the
    # cumulative bound for the rest of the document. PDFium sanitises its own
    # output today, so nothing reaches this; it is guarded because the bound is
    # documented and tested as a standalone function, and its next caller will
    # not know that:
    if not math.isfinite(width or 0) or not math.isfinite(height or 0):
        return 0.0
    longest = max(width or 0, height or 0)
    if longest <= 0:
        return 0.0
    return min(base_scale, _RENDER_MAX_PX / longest)


def disarm_pdf_to_safe_bytes(
    data: bytes,
    *,
    rebuild: Optional[Callable[[bytes], bytes]] = None,
    flatten: bool = False,
    max_bytes: Optional[int] = None,
) -> bytes:
    """Rebuilding an uploaded PDF as a safe new PDF, or raising.

    With no override, this runs the ladder documented at module level:
    allowlist rebuild -> text-layered raster -> plain raster -> refuse. Only
    the last step raises for a reason other than "this is not a readable PDF
    at all" combined with the upstream empty/size/magic checks below - every
    rung in between degrades to the next one rather than surfacing a failure
    to the caller.

    `flatten=True` skips straight to the plain-raster rung, for a caller whose
    threat model wants rasterisation unconditionally rather than as a
    fallback (it is not used anywhere in the product upload path today).

    `rebuild=` is an injectable test seam that REPLACES the ladder entirely -
    it is for faking the rebuild step without PDFium/pikepdf in unit tests,
    and a seam that fails or echoes the input still raises, because a test
    seam standing in for "the whole disarm step is broken" is not a case the
    product-facing degrade ladder is meant to paper over.

    Never returns `data` unchanged. On any failure raises
    `InvalidDocumentException` or `DocumentDisarmFailedException`.
    """
    from platform.exceptions import DocumentDisarmFailedException, InvalidDocumentException

    # The ONE tunable this ladder reads, threaded in rather than imported when a
    # caller supplies it. `sandbox.py` runs this whole module in a process with a
    # scrubbed environment and no credentials, where `infrastructure.settings`
    # cannot construct at all (it requires DATABASE_URL); passing the integer is
    # what keeps the parser free of any reason to touch settings. In-process
    # callers pass nothing and get the configured value exactly as before.
    max_bytes = _resolve_max_bytes(max_bytes)

    if not data:
        raise InvalidDocumentException("The uploaded file is empty")
    if len(data) > max_bytes:
        raise InvalidDocumentException("The file is too large")
    # Scanned for in a WINDOW rather than required at byte zero. The spec puts
    # the header first and real files routinely do not: a UTF-8 BOM from a
    # naive re-save, a few bytes of transfer junk, a wrapper that prepended
    # something. Measured rather than assumed - with each of those prefixes,
    # BOTH readers this backend uses open the document without complaint, qpdf
    # via pikepdf and PDFium alike. A gate stricter than the parsers it exists
    # to protect refuses documents that would have worked, and on this path
    # that costs a member their CV outright, because nobody re-exports a file
    # their own viewer opens fine. 1KB is the window readers conventionally
    # scan, and it is deliberately not "anywhere in the file": a PDF signature
    # buried in the middle of some other format is that format quoting a PDF,
    # which no reader would open either. The product-side gate
    # (`talent_service._PDF_HEADER_WINDOW`) uses the same number.
    if b"%PDF-" not in data[:_PDF_HEADER_WINDOW]:
        raise InvalidDocumentException("Only PDF files are accepted")

    if rebuild is not None:
        try:
            safe = rebuild(data)
        except (InvalidDocumentException, DocumentDisarmFailedException):
            raise
        except Exception as exc:
            logger.warning("Custom PDF rebuild seam failed", exc_info=True)
            raise DocumentDisarmFailedException(
                "This PDF could not be processed safely. Please export a new PDF and try again."
            ) from exc
    elif flatten:
        _assert_raster_cost_is_bounded(data)
        safe = _raster_to_size_budget(data, with_text_layer=False, max_bytes=max_bytes)
    else:
        safe = _rebuild_via_ladder(data, max_bytes=max_bytes)

    if not safe or not safe.startswith(b"%PDF-"):
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please export a new PDF and try again."
        )
    # Construction check: we must never "succeed" by echoing the upload:
    if safe == data:
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please export a new PDF and try again."
        )
    if len(safe) > max_bytes:
        raise DocumentDisarmFailedException(
            "The safe copy of this PDF is too large. Please upload a shorter CV."
        )
    return safe


def _is_resource_exhaustion(exc: BaseException) -> bool:
    """Whether a rung-1 failure was about COST, by walking the cause chain.

    `rebuild.py` tags its budget breaches rather than raising a distinct type,
    so the user-facing exception and message every existing caller sees stay
    exactly as they were.
    """
    from platform.services.document_ingest.rebuild import ResourceExhausted

    seen = set()
    cause: Optional[BaseException] = exc
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, ResourceExhausted):
            return True
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return False


# The ceiling on how much DECODED content stream this module will hand to a
# renderer. Generous by design: a real CV's page content decodes to well under
# 2MB in every file measured here, so 64MB is roughly thirty times the headroom
# a legitimate document needs.
#
# It exists because rung 1's cost tag turned out not to cover the case that
# matters most. That rung decides "can I rebuild this?" BEFORE it asks "what
# does this cost?", so a content stream carrying a filter it will not decode -
# an LZW chain, or a /DecodeParms /Predictor - is declined as a COMPATIBILITY
# problem while nothing at all is known about its size. The ladder then does
# exactly what it should for a compatibility gap and rasterises it, and PDFium
# executes every operator in a single uninterruptible call that no
# between-pages wall-clock check can reach.
#
# Measured, and this is why it blocks a merge rather than being a note: a
# 390KB upload whose one page draws 200 million glyphs cost 11.5 seconds and
# 988MB of RSS; at 973KB it cost 40 seconds and 2.1GB, past the gunicorn worker
# timeout of 30 and past the whole 2GB instance, with two such uploads allowed
# to run concurrently. So cost has to be established independently of
# rebuildability, before any renderer is asked to run.
_MAX_RASTER_CONTENT_BYTES = 64 * 1024 * 1024

# ...and the ceiling on RAW content bytes for a stream this module cannot
# decode itself, where the decoded size can only be learned by asking qpdf.
# Anything within this is a spike we can afford to take once in order to
# measure; anything past it is refused unmeasured.
_MAX_UNMEASURABLE_RAW_BYTES = 2 * 1024 * 1024


def _assert_raster_cost_is_bounded(data: bytes) -> None:
    """Raise (tagged as cost) if rasterising this document is unaffordable.

    Deliberately measures rather than trusts. The cheap half is our own capped
    inflate, which covers every real generator; the expensive half asks qpdf,
    and only for a stream small enough that the answer is worth the spike.
    A stream nobody can decode is not a cost signal at all, so it is passed
    over: PDFium will fail fast on genuine garbage, and refusing a document
    because we could not read one of its streams is the false positive this
    whole ladder exists to avoid.
    """
    from platform.services.document_ingest import rebuild as rb

    import pikepdf

    total = 0
    try:
        source = pikepdf.open(io.BytesIO(data))
    except Exception:
        # Not openable here is not a verdict: PDFium may still render it, and
        # deciding otherwise would refuse on our own reader's limitations.
        return
    with source:
        for page in source.pages:
            contents = page.obj.get("/Contents")
            if contents is None:
                continue
            streams = contents if isinstance(contents, pikepdf.Array) else [contents]
            for stream in streams:
                total += _decoded_size_estimate(rb, pikepdf, stream)
                if total > _MAX_RASTER_CONTENT_BYTES:
                    raise rb.cost_refusal(
                        "This PDF is too large to process safely. Please upload a shorter CV."
                    )


def _decoded_size_estimate(rb, pikepdf, stream) -> int:
    """Decoded bytes for one content stream, or 0 when unknowable."""
    if not isinstance(stream, pikepdf.Stream):
        return 0
    try:
        raw = bytes(stream.read_raw_bytes())
    except Exception:
        return 0
    filters = rb.filter_names(stream)
    # The predictor is irrelevant to SIZE - it is a per-row transform applied
    # after inflation - so it is ignored here even though rung 1 refuses on it.
    # That is the whole point: the two questions are separate.
    if filters is not None and all(name == rb.FLATE for name in filters):
        try:
            return len(rb.inflate_capped(raw, _MAX_RASTER_CONTENT_BYTES + 1))
        except Exception:
            return _MAX_RASTER_CONTENT_BYTES + 1
    if not filters:
        return len(raw)
    if len(raw) <= _MAX_UNMEASURABLE_RAW_BYTES:
        try:
            return len(bytes(stream.read_bytes()))
        except Exception:
            return 0
    return _MAX_RASTER_CONTENT_BYTES + 1


def _rebuild_via_ladder(data: bytes, *, max_bytes: Optional[int] = None) -> bytes:
    """Rungs 1-3, first success wins; rung 3's own failure is the only refusal.

    Every `except Exception` below is deliberate and not a style lapse: each
    rung's job is to hand a real CV back rather than an error, so ANY failure
    mode of a rung - a known budget exception, an unexpected bug in a
    third-party PDF library, a corrupt-but-openable stream - is treated the
    same way, as a reason to try the next, more tolerant rung. `rebuild.py`'s
    own budget exceptions are a perfect example: they are real and correct
    signals that THAT rung cannot handle this document, not signals that the
    document itself is unsalvageable.
    """
    from platform.exceptions import DocumentDisarmFailedException

    try:
        return rebuild_pdf_preserving_text(data)
    except Exception as exc:
        # The ONE case where falling through is worse than refusing. Rung 1
        # declines for two very different reasons, and only one of them says
        # anything about what the next rung will cost:
        #
        # - COMPATIBILITY (an LZW filter, an object shape it will not read).
        #   The document may be perfectly ordinary; rasterising is exactly
        #   right, and that is the whole point of the ladder.
        # - COST (its byte or object budget). The content is pathologically
        #   large, and PDFium does not escape that by being asked instead --
        #   it pays MORE, because it executes the operators rather than
        #   counting the bytes. Measured on this checkout: a 2GB content
        #   stream is declined here in 0.1s and then takes 70 SECONDS to
        #   rasterise, against a gunicorn worker timeout of 30. Falling
        #   through does not save that upload; it kills the worker and takes
        #   every other in-flight request on it down as collateral.
        #
        # So a cost failure stops here. That is not a member being refused: no
        # CV has a 2GB content stream, and a file that does is a
        # resource-exhaustion payload wearing a document's clothes.
        if _is_resource_exhaustion(exc):
            logger.warning(
                "Disarm ladder rung 1 declined for COST rather than "
                "compatibility; refusing rather than handing a "
                "resource-exhaustion payload to a slower renderer",
                exc_info=True,
            )
            raise
        logger.warning(
            "Disarm ladder rung 1 (allowlist rebuild) could not handle this "
            "document; degrading to rasterisation",
            exc_info=True,
        )

    # Before ANY renderer runs. See `_assert_raster_cost_is_bounded`: rung 1
    # declining for compatibility says nothing about cost, and the renderer is
    # where an unbounded cost actually gets paid.
    _assert_raster_cost_is_bounded(data)

    try:
        return _raster_to_size_budget(data, with_text_layer=True, max_bytes=max_bytes)
    except Exception:
        logger.warning(
            "Disarm ladder rung 2 (text-layered raster) could not handle this "
            "document; degrading to plain rasterisation",
            exc_info=True,
        )

    try:
        safe = _raster_to_size_budget(data, with_text_layer=False, max_bytes=max_bytes)
    except Exception as exc:
        logger.warning(
            "Disarm ladder rung 3 (plain raster) could not handle this "
            "document either; refusing the upload",
            exc_info=True,
        )
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please export a new PDF and try again."
        ) from exc

    return safe


# ---------------------------------------------------------------------------
# Rasterisation shared by rungs 2 and 3
# ---------------------------------------------------------------------------


def _render_pages(data: bytes, *, dpi: float) -> list[Image.Image]:
    """Every page of `data` as an RGB PIL image, rendered at `dpi`.

    Degrades rather than refuses on every budget in this module EXCEPT the
    per-page pixel ceiling (`_RENDER_MAX_PX`, applied via `_clamped_scale`,
    which cannot degrade because the allocation it bounds is atomic) and the
    case where PDFium cannot open the document, or it genuinely has no pages -
    at that point there is no page to render at any dpi, so this raises and
    lets the ladder try the next rung (which will hit the same wall, and
    ultimately the ladder refuses).
    """
    from platform.exceptions import DocumentDisarmFailedException

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise DocumentDisarmFailedException(
            "Safe PDF processing is unavailable on this server. Please try again later."
        ) from exc

    base_scale = dpi / 72.0
    started = time.monotonic()
    tmp_path: Optional[Path] = None
    images: list[Image.Image] = []
    try:
        with tempfile.NamedTemporaryFile(prefix="pdf-og-", suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        try:
            document = pdfium.PdfDocument(str(tmp_path))
        except Exception as exc:
            raise DocumentDisarmFailedException(
                "This PDF could not be opened. If it is password-protected, export an "
                "unencrypted copy and try again."
            ) from exc

        try:
            page_count = len(document)
            if page_count < 1:
                raise DocumentDisarmFailedException("This PDF has no pages to share.")
            pages_to_try = min(page_count, _MAX_PAGES)
            if page_count > _MAX_PAGES:
                logger.warning(
                    "Document declares %d pages, past the %d sanity ceiling; "
                    "rendering the first %d rather than refusing the upload",
                    page_count, _MAX_PAGES, _MAX_PAGES,
                )

            total_pixels = 0.0
            for index in range(pages_to_try):
                if time.monotonic() - started > _MAX_WALL_SECONDS:
                    logger.warning(
                        "Raster rung hit the %.0fs wall-clock budget after %d of "
                        "%d pages; degrading to a partial document rather than "
                        "refusing the upload",
                        _MAX_WALL_SECONDS, len(images), page_count,
                    )
                    break
                page = document[index]
                try:
                    width, height = page.get_size()
                    scale = _clamped_scale(width, height, base_scale)
                    if scale <= 0:
                        # A degenerate declared size (zero, negative, NaN):
                        # there is nothing to render, but the alternative to a
                        # blank stand-in page is dropping the page entirely,
                        # which would silently reorder or truncate a document
                        # a human will read start to finish. A blank US-Letter
                        # page keeps the page COUNT and ORDER honest at the
                        # cost of that one page's content, which the source
                        # PDF already made unrenderable:
                        logger.warning(
                            "Page %d of %d has no usable size; degrading to a "
                            "blank page in place of its content",
                            index + 1, page_count,
                        )
                        image = Image.new(
                            "RGB", (int(8.5 * dpi), int(11.0 * dpi)), "white"
                        )
                    else:
                        page_pixels = (width * scale) * (height * scale)
                        # Only refuse to render a page over the pixel budget if
                        # something has already been rendered - a single page
                        # alone is never held back, because the per-page clamp
                        # above already bounds it to something the worker can
                        # afford on its own; a one-page CV must never come back
                        # with zero pages just because that one page's clamped
                        # cost exceeds a budget sized for a multi-page document:
                        if images and total_pixels + page_pixels > _MAX_TOTAL_PIXELS:
                            logger.warning(
                                "Raster rung hit the %d total-pixel budget "
                                "after %d of %d pages; degrading to a partial "
                                "document rather than refusing the upload",
                                _MAX_TOTAL_PIXELS, len(images), page_count,
                            )
                            page.close()
                            break
                        total_pixels += page_pixels
                        bitmap = page.render(scale=scale)
                        image = bitmap.to_pil().convert("RGB")
                finally:
                    page.close()
                images.append(image)
                if time.monotonic() - started > _MAX_WALL_SECONDS:
                    logger.warning(
                        "Raster rung hit the %.0fs wall-clock budget right "
                        "after page %d of %d; degrading to a partial document "
                        "rather than refusing the upload",
                        _MAX_WALL_SECONDS, len(images), page_count,
                    )
                    break
        finally:
            document.close()
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to unlink ephemeral OG temp file", exc_info=True)

    if not images:
        raise DocumentDisarmFailedException("This PDF has no pages to share.")
    return images


def _assemble_plain_raster(images: list[Image.Image], *, dpi: float, quality: int) -> bytes:
    """Every page as a JPEG-encoded page inside one PDF. See `_JPEG_QUALITY`
    for why JPEG replaced Pillow's Flate-encoded PDF-save default."""
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=images[1:],
        resolution=dpi,
        quality=quality,
    )
    return buffer.getvalue()


def _assemble_text_layer(images: list[Image.Image], *, dpi: float, source_data: bytes) -> bytes:
    """Rung 2: hand the rendered pages to the text-layer module (owned
    elsewhere in this package) to re-embed the original's text over them."""
    from platform.services.document_ingest.textlayer import raster_with_text_layer

    return raster_with_text_layer(images, source_data, dpi=dpi)


def _raster_to_size_budget(
    data: bytes, *, with_text_layer: bool, max_bytes: Optional[int] = None
) -> bytes:
    """Render + assemble, stepping the DPI ladder down until the result fits
    `MAX_DOCUMENT_BYTES`, and dropping trailing pages as a last resort.

    This is the mechanism behind "rung 2/3 output must stay under
    MAX_DOCUMENT_BYTES or step the DPI down and re-render rather than
    refusing": a document that is oversized purely because of page count or
    per-page density gets smaller at a lower DPI (fewer pixels per page, so
    both the JPEG/text-layer encode AND the total-pixel budget in
    `_render_pages` allow more pages through). If even the bottom of the DPI
    ladder does not fit, dropping trailing pages one at a time guarantees SOME
    document is produced - a truncated CV over no CV at all - short of the
    pathological case of a single page whose own content is denser than the
    entire size budget, which is the one raster-side case this module still
    lets fail through to the ladder's final refusal.
    """
    max_bytes = _resolve_max_bytes(max_bytes)

    last_images: Optional[list[Image.Image]] = None
    for dpi in _RASTER_DPI_LADDER:
        images = _render_pages(data, dpi=dpi)
        last_images = images
        if with_text_layer:
            safe = _assemble_text_layer(images, dpi=dpi, source_data=data)
        else:
            safe = _assemble_plain_raster(images, dpi=dpi, quality=_JPEG_QUALITY)
        if len(safe) <= max_bytes:
            return safe
        logger.warning(
            "Raster output at %.0f dpi was %d bytes, over the %d-byte cap; "
            "stepping the render DPI down and trying again",
            dpi, len(safe), max_bytes,
        )

    # Every DPI in the ladder produced an oversized document. `last_images`
    # holds the bottom-of-ladder (smallest) render; trim it from the end
    # rather than refusing outright - a CV missing its last page or two is
    # still a CV a recruiter can open, which is the entire point of this
    # module existing.
    images = last_images or []
    floor_dpi = _RASTER_DPI_LADDER[-1]
    while len(images) > 1:
        images = images[: len(images) - 1]
        if with_text_layer:
            safe = _assemble_text_layer(images, dpi=floor_dpi, source_data=data)
        else:
            safe = _assemble_plain_raster(images, dpi=floor_dpi, quality=_JPEG_QUALITY)
        if len(safe) <= max_bytes:
            logger.warning(
                "Dropped to the first %d page(s) at %.0f dpi to fit the "
                "%d-byte cap rather than refusing the upload",
                len(images), floor_dpi, max_bytes,
            )
            return safe

    # A single page, at the smallest DPI and (for rung 3) the standing JPEG
    # quality, still will not fit. This is the one raster-side outcome this
    # module does not try to talk its way out of: there is no more DPI to
    # drop and no more pages to trim. It raises here, which the ladder (for
    # rung 2) turns into a fall-through to rung 3, and (for rung 3, the
    # bottom of the ladder) into the final refusal.
    raise RuntimeError(
        "Rasterised output exceeds the size budget even at the lowest DPI and a single page"
    )
