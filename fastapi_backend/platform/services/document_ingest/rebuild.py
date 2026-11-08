"""Allowlist rebuild: as-received PDF → a new PDF carrying only page content.

The safety argument here is not "we removed the dangerous parts" — it is that
the dangerous parts are never READ. A denylist has to enumerate every place an
attack can hide, and PDF has too many: this repo's own `document_sanitize`
missed inline action dictionaries under unenumerated keys, and so did the first
draft of this module, because `for obj in pdf.objects` yields only INDIRECT
objects. The property that replaces that enumeration is mechanical:

    rebuild(clean) == rebuild(clean + any_amount_of_attack)   byte for byte

An attacker's additions live in the catalogue, in annotations, in AcroForm, in
names trees, in outlines — none of which this module opens. It walks page
geometry, the content-stream bytes, and the resource sub-tree, copying only
keys named below. Anything unnamed is not stripped; it is simply never visited.
`test_document_rebuild.py` asserts the equality above against a corpus of
attack classes, so the claim is checked rather than argued.

That property is only worth as much as the walk is thorough, and the first
version of this module did not earn it. It filtered the page dictionary and
Form XObjects by name, then handed every OTHER resource — every ExtGState,
Pattern, Shading, ColorSpace and Font — to `copy_foreign`, which copies an
object and its whole referenced subgraph VERBATIM. An adversarial pass proved
the consequence: an `/OpenAction` `/JavaScript` dictionary parked inside an
ExtGState survived into the "safe" output untouched, and the byte-identity
property was simply false. So every category now has its own key list and its
own copier, and `copy_foreign` is not called anywhere in this file. Streams are
carried as bytes we decoded ourselves (or, for image data, as raw bytes under a
filter list we recognise), never as an object handed over whole.

Compared with rasterising, this keeps the text layer (so a CV stays
searchable, selectable, screen-reader-readable and ATS-parseable) at a
fraction of the memory. What it does NOT do is remove the viewer's font and
image parsers from the picture — that residual surface is real and is stated
in DOCS.md rather than hidden.
"""

from __future__ import annotations

import binascii
import io
import logging
import time
import zlib
from typing import Optional


logger = logging.getLogger(__name__)

# Keys copied from a source PAGE. Everything else — /Annots (links, widgets,
# screens, file attachments), /AA (additional actions), /B, /Metadata — is
# never read. MediaBox is the only geometry that must survive for the page to
# lay out; Rotate and the crop/trim boxes are visual and safe scalars.
_PAGE_KEYS = ("/MediaBox", "/CropBox", "/Rotate", "/UserUnit")

# Resource categories a content stream can legitimately reference. Notably
# absent: nothing else is admitted, so a category invented by an attacker (or
# added by a future PDF revision) is dropped by default rather than by rule.
_RESOURCE_KEYS = ("/Font", "/XObject", "/ExtGState", "/ColorSpace", "/Pattern", "/Shading", "/ProcSet")

# Within a Form XObject, the keys that make it draw. /Ref is deliberately
# excluded: a "reference XObject" points at an external PDF file, which is an
# outbound reference wearing the costume of a drawing primitive. So is
# /PieceInfo, which is private application data with no drawing role at all.
_FORM_KEYS = ("/BBox", "/Matrix", "/Group", "/FormType")

# A transparency group. /S is always /Transparency here; the rest is colour.
_GROUP_KEYS = ("/Type", "/S", "/CS", "/I", "/K")

# Simple, CID and Type3 font dictionaries share one list; a key that does not
# apply to a given subtype is simply absent from that font.
_FONT_KEYS = (
    "/Type", "/Subtype", "/Name", "/BaseFont", "/FirstChar", "/LastChar",
    "/Widths", "/Encoding", "/FontDescriptor", "/DescendantFonts", "/ToUnicode",
    "/CIDSystemInfo", "/CIDToGIDMap", "/W", "/W2", "/DW", "/DW2",
    "/FontMatrix", "/FontBBox", "/CharProcs", "/Resources",
)

# Metrics and the embedded programme itself. /FontFile* are opaque binaries the
# viewer's font parser reads — the residual surface named in the module
# docstring, carried deliberately because losing them loses the typeface.
_FONT_DESCRIPTOR_KEYS = (
    "/Type", "/FontName", "/FontFamily", "/FontStretch", "/FontWeight", "/Flags",
    "/FontBBox", "/ItalicAngle", "/Ascent", "/Descent", "/Leading", "/CapHeight",
    "/XHeight", "/StemV", "/StemH", "/AvgWidth", "/MaxWidth", "/MissingWidth",
    "/CharSet", "/Lang", "/Style", "/FD", "/CIDSet",
    "/FontFile", "/FontFile2", "/FontFile3",
)

# An /Encoding dictionary: a base encoding plus a difference table of names.
_ENCODING_KEYS = ("/Type", "/BaseEncoding", "/Differences")

# Image XObject geometry and sample interpretation. /Alternates is excluded (a
# second image the viewer may substitute), as is /OPI (an external-file
# reference) and /OC (optional-content, which reaches a catalogue tree).
_IMAGE_KEYS = (
    "/Type", "/Subtype", "/Width", "/Height", "/BitsPerComponent", "/ColorSpace",
    "/Decode", "/Interpolate", "/ImageMask", "/Intent", "/SMask", "/Mask",
    "/SMaskInData", "/Matte",
)

# Graphics state. Every one of these is a rendering parameter; the two that
# reach further — /SMask (a soft mask holding a Form) and the transfer/halftone
# function slots — get their own copier below.
_EXTGSTATE_KEYS = (
    "/Type", "/LW", "/LC", "/LJ", "/ML", "/D", "/RI", "/OP", "/op", "/OPM",
    "/Font", "/BM", "/SMask", "/CA", "/ca", "/AIS", "/TK", "/FL", "/SA",
    "/BG", "/BG2", "/UCR", "/UCR2", "/TR", "/TR2", "/HT", "/HTO",
)
_SOFT_MASK_KEYS = ("/Type", "/S", "/G", "/BC", "/TR")

_TILING_PATTERN_KEYS = (
    "/Type", "/PatternType", "/PaintType", "/TilingType", "/BBox", "/XStep",
    "/YStep", "/Resources", "/Matrix",
)
_SHADING_PATTERN_KEYS = ("/Type", "/PatternType", "/Matrix", "/ExtGState", "/Shading")

_SHADING_KEYS = (
    "/ShadingType", "/ColorSpace", "/Background", "/BBox", "/AntiAlias",
    "/Coords", "/Domain", "/Extend", "/Function", "/Matrix", "/Type",
    "/BitsPerCoordinate", "/BitsPerComponent", "/BitsPerFlag", "/Decode",
    "/VerticesPerRow",
)

_FUNCTION_KEYS = (
    "/FunctionType", "/Domain", "/Range", "/C0", "/C1", "/N", "/Functions",
    "/Bounds", "/Encode", "/Decode", "/Size", "/BitsPerSample", "/Order",
)

# An ICC profile stream, or the lookup stream of an /Indexed space.
_COLORSPACE_STREAM_KEYS = ("/N", "/Alternate", "/Range", "/Filter", "/DecodeParms")

_HALFTONE_KEYS = (
    "/Type", "/HalftoneType", "/HalftoneName", "/Frequency", "/Angle",
    "/SpotFunction", "/AccurateScreens", "/TransferFunction", "/Width",
    "/Height", "/Thresholds", "/Xsquare", "/Ysquare",
)

# How deep a Form XObject may nest inside another. Forms can reference forms,
# so an attacker can build a cycle or a bomb out of legitimate primitives.
_MAX_FORM_DEPTH = 8
# ...and the same for the object graph generally. Every copier recurses, and a
# resource dictionary can be made self-referential; the memo does not stop that
# on its own, because it is only populated once a subtree is finished.
_MAX_DEPTH = 24

# Resource budgets. The rasterising path has carried these since a 258-byte
# upload was shown to cost 3.6GB; this path had NONE, on the assumption that
# not rendering anything made it cheap. It does not: a 120KB upload whose
# content stream inflates to 60MB was measured at 511MB peak RSS, and the shape
# scales linearly, so a file inside the 10MB upload cap reaches multiple GB.
# The call site's concurrency limiter bounds how MANY rebuilds run, never what
# one costs, so the bound has to live here.
# Not a refusal any more: the ladder in `disarm.py` rasterises whatever this
# rung declines, and a rasterised CV is strictly worse than a text-preserving
# one on every axis that matters (searchable, ATS-parseable, and roughly forty
# times smaller). Declining a 60-page academic CV here used to hand it to the
# raster rung, which then truncated it to 43 pages under its pixel budget: the
# member silently lost seventeen pages AND their text layer, to satisfy a cap
# that never described what this rung could actually afford. The real costs are
# bounded by the byte and object budgets below, which is where they belong.
# 500 is a sanity ceiling on loop cost alone.
_MAX_PAGES = 500
_MAX_WALL_SECONDS = 20.0
# Total DECODED bytes across every stream this module inflates — page contents,
# form and pattern contents, font programmes, ToUnicode maps. Measured on this
# checkout: a file decoding to 31MB (just inside the budget) peaks at 95MB over
# baseline, so roughly 3x the budget, and two concurrent rebuilds cost under
# 200MB against the 2GB production instance. A file decoding to 2GB is now
# refused in 0.1s at 73MB over baseline; before this bound it inflated the whole
# thing first. A text-heavy 40-page CV decodes to a couple of MB:
_MAX_DECODED_BYTES = 32 * 1024 * 1024
# Total RAW bytes of streams passed through without decoding (image samples).
# These are bounded by the upload cap in practice; the budget stops one file
# from being nothing but a hundred references to the same enormous image:
_MAX_RAW_BYTES = 24 * 1024 * 1024
# A crafted file can name millions of tiny resources; each costs an allocation
# in the output even though none of them is individually large:
_MAX_OBJECTS = 50_000

# Stream filters this module is willing to decode itself. Flate is the only one
# with an unbounded expansion ratio, and it is the only one that matters in
# practice, so it gets an incremental capped inflate. The ASCII filters shrink
# their input; RunLength is capped as it runs. LZW is absent deliberately: it
# is also unbounded, essentially extinct in generators written this century,
# and adding a capped decoder for it would be more attack surface than the
# compatibility is worth. A file using it is refused, not silently mangled.
_ASCII_HEX = "/ASCIIHexDecode"
_ASCII_85 = "/ASCII85Decode"
_FLATE = "/FlateDecode"
_RUN_LENGTH = "/RunLengthDecode"
_DECODABLE_FILTERS = (_ASCII_HEX, _ASCII_85, _FLATE, _RUN_LENGTH)


class ResourceExhausted(Exception):
    """Raised when this rung declines for COST rather than for compatibility.

    The ladder in `disarm.py` treats those two failures very differently, and
    conflating them is dangerous in a way that only shows up under attack. A
    compatibility gap (an LZW filter, an object shape this module will not
    read) says nothing about how expensive the document is, so falling through
    to rasterisation is exactly right. Resource exhaustion says the opposite:
    the content is pathologically large, and PDFium will pay that cost too,
    only slower, because it executes the operators rather than merely counting
    the bytes. Measured: a 2GB content stream is declined here in 0.1s and then
    took 70 SECONDS to rasterise, against a gunicorn worker timeout of 30 —
    so falling through turned a clean refusal into a killed worker that takes
    every other in-flight request on it down as collateral.

    This is the one input class the never-refuse rule does not cover, and the
    distinction is deliberate: no CV has a 2GB content stream. A file like that
    is a resource-exhaustion payload wearing a document's clothes, and refusing
    it is refusing an attack rather than refusing a member.
    """


def _tagged(exc, reason: str):
    """Hang a `ResourceExhausted` on the exception's cause chain.

    A tag rather than a new exception type so every existing caller and test
    keeps the exact user-facing exception and message it already had, while
    `disarm.py` can still ask "was this cost or compatibility?" by walking
    `__cause__`.
    """
    exc.__cause__ = ResourceExhausted(reason)
    return exc


class _Budget:
    """The per-rebuild spend, so every copier can charge against one ceiling."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.decoded = 0
        self.raw = 0
        self.objects = 0

    def _fail(self, message: str):
        """A budget breach, tagged as COST so the ladder does not rasterise it.

        `ResourceExhausted` is chained onto the user-facing exception rather
        than replacing it: callers that just want to refuse keep the message
        they always had, and `disarm.py` looks for the tag on the chain.
        """
        from platform.exceptions import DocumentDisarmFailedException

        return _tagged(DocumentDisarmFailedException(message), message)

    def tick(self) -> None:
        self.objects += 1
        if self.objects > _MAX_OBJECTS:
            raise self._fail(
                "This PDF is too complex to process safely. Please upload a simpler CV."
            )
        # Checked on every object rather than once per page: an operator-dense
        # single page is exactly the case a per-page check would miss.
        if time.monotonic() - self.started > _MAX_WALL_SECONDS:
            raise self._fail(
                "Processing this PDF took too long. Please upload a simpler or shorter file."
            )

    def spend_decoded(self, count: int) -> None:
        self.decoded += count
        if self.decoded > _MAX_DECODED_BYTES:
            raise self._fail(
                "This PDF is too large to process safely. Please upload a shorter CV."
            )

    def spend_raw(self, count: int) -> None:
        self.raw += count
        if self.raw > _MAX_RAW_BYTES:
            raise self._fail(
                "This PDF is too large to process safely. Please upload a shorter CV."
            )

    def remaining_decoded(self) -> int:
        return max(_MAX_DECODED_BYTES - self.decoded, 0)


class _Ctx:
    def __init__(self, pikepdf, out) -> None:
        self.pikepdf = pikepdf
        self.out = out
        self.budget = _Budget()
        # Memoised per (source object, role). The role is part of the key
        # because the same object reached as two different roles is filtered by
        # two different key lists, and reusing the first result would leak the
        # wider one:
        self.seen: dict[tuple, object] = {}


def rebuild_pdf_preserving_text(data: bytes) -> bytes:
    """A new PDF containing every page's marks and nothing else.

    Raises `DocumentDisarmFailedException` on anything it cannot rebuild
    safely; never returns the input.
    """
    from platform.exceptions import DocumentDisarmFailedException

    import pikepdf

    try:
        source = pikepdf.open(io.BytesIO(data))
    except Exception as exc:
        raise DocumentDisarmFailedException(
            "This PDF could not be opened. If it is password-protected, export an "
            "unencrypted copy and try again."
        ) from exc

    try:
        with source, pikepdf.new() as out:
            page_count = len(source.pages)
            if page_count < 1:
                raise DocumentDisarmFailedException("This PDF has no pages to share.")
            if page_count > _MAX_PAGES:
                raise DocumentDisarmFailedException(
                    f"This PDF has too many pages (maximum {_MAX_PAGES}). "
                    "Please upload a shorter CV."
                )

            ctx = _Ctx(pikepdf, out)
            for source_page in source.pages:
                out.pages.append(pikepdf.Page(_rebuilt_page(ctx, source_page)))

            buffer = io.BytesIO()
            # Rebuilt object streams and a fresh xref: nothing of the source
            # file's own structure (incremental updates, stale xref entries,
            # bytes after %%EOF) carries over.
            # deterministic_id makes the output a pure function of the marks we
            # copied. Two reasons, and the second is the important one: without
            # it qpdf derives the trailer /ID partly from the clock, so the same
            # CV uploaded twice yields different bytes and a different sha256 —
            # and, more to the point, the "identical output for clean and
            # hostile input" property this module rests on could only be
            # asserted within the same instant. With it, that equality is exact
            # and a test can hold it in CI.
            out.save(
                buffer,
                linearize=False,
                deterministic_id=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
            return buffer.getvalue()
    except DocumentDisarmFailedException:
        raise
    except Exception as exc:
        logger.warning("PDF allowlist rebuild failed", exc_info=True)
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please export a new PDF and try again."
        ) from exc


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _rebuilt_page(ctx: _Ctx, source_page):
    """One page, carrying its marks and the resources they name."""
    pikepdf = ctx.pikepdf
    ctx.budget.tick()
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page)

    for key in _PAGE_KEYS:
        value = _inherited(source_page.obj, key)
        if value is not None:
            copied = _plain(ctx, value, 0)
            if copied is not None:
                page[key] = copied
    if "/MediaBox" not in page:
        page.MediaBox = pikepdf.Array([0, 0, 612, 792])

    # The marks themselves, handed over DECODED. The source's filter chain is
    # attacker-chosen, and a viewer that must run it is a decoder the attacker
    # selected; re-encoding on save means only our own choice reaches them.
    page.Contents = ctx.out.make_stream(_content_bytes(ctx, source_page))

    page.Resources = _copy_resources(ctx, _inherited(source_page.obj, "/Resources"), 0)
    return ctx.out.make_indirect(page)


def _inherited(page_obj, key):
    """A page attribute, resolved up the /Pages tree.

    /Resources, /MediaBox, /CropBox and /Rotate are INHERITABLE: a generator may
    declare shared fonts once on the parent node and omit them from every page,
    which Word and LaTeX both do. A page whose fonts resolved to nothing would
    rebuild to a blank sheet while its content stream still nominally names
    them — silent and total.

    Measured: qpdf already pushes inherited attributes down to each leaf when
    it opens a file, so `page.obj` carries them even for a hand-built PDF that
    declares them only on /Pages. This walk is therefore belt-and-braces, not a
    bug fix, and it is kept because the alternative is depending on that
    normalisation staying true across a qpdf upgrade for a failure mode that is
    invisible when it happens. The depth bound stops a self-referential /Parent
    chain from spinning.
    """
    node = page_obj
    for _ in range(32):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _content_bytes(ctx: _Ctx, source_page) -> bytes:
    """Every content stream of a page, concatenated and decompressed."""
    pikepdf = ctx.pikepdf
    contents = source_page.obj.get("/Contents")
    if contents is None:
        return b""
    streams = contents if isinstance(contents, pikepdf.Array) else [contents]
    parts = []
    for stream in streams:
        decoded = _decoded_stream_bytes(ctx, stream)
        if decoded is not None:
            parts.append(decoded)
    return b"\n".join(parts)


# ---------------------------------------------------------------------------
# Stream bytes
# ---------------------------------------------------------------------------


def _filter_names(ctx: _Ctx, stream) -> Optional[list[str]]:
    """The stream's filter chain as plain strings, or None if unreadable."""
    pikepdf = ctx.pikepdf
    raw = stream.get("/Filter")
    if raw is None:
        return []
    if isinstance(raw, pikepdf.Array):
        names = list(raw)
    else:
        names = [raw]
    out = []
    for name in names:
        if not isinstance(name, pikepdf.Name):
            return None
        out.append(str(name))
    return out


def _decoded_stream_bytes(ctx: _Ctx, stream) -> Optional[bytes]:
    """A stream's decoded bytes, refusing anything this module cannot bound.

    pikepdf's own `read_bytes` decodes the whole stream before anyone can look
    at the size, which is the entire mechanism of a decompression bomb: the
    measurement that would refuse the file only exists once the memory has been
    spent. So the chain is walked here instead, with a cap threaded through
    each stage.
    """
    pikepdf = ctx.pikepdf
    if not isinstance(stream, pikepdf.Stream):
        return None
    filters = _filter_names(ctx, stream)
    if filters is None or any(name not in _DECODABLE_FILTERS for name in filters):
        raise _unsupported_filter()
    # A predictor is an image/xref-stream construct; on a content, font or
    # ToUnicode stream it is either a mistake or an attempt to reach a decoder
    # this module does not run. Refusing is fail-closed and costs nothing real:
    if _has_predictor(ctx, stream):
        raise _unsupported_filter()

    data = bytes(stream.read_raw_bytes())
    ctx.budget.spend_raw(len(data))
    for name in filters:
        limit = ctx.budget.remaining_decoded()
        if name == _ASCII_HEX:
            data = _ascii_hex_decode(data)
        elif name == _ASCII_85:
            data = _ascii85_decode(data)
        elif name == _FLATE:
            data = _inflate_capped(data, limit)
        elif name == _RUN_LENGTH:
            data = _run_length_decode(data, limit)
        if len(data) > limit:
            raise _too_big()
    ctx.budget.spend_decoded(len(data))
    return data


def _has_predictor(ctx: _Ctx, stream) -> bool:
    pikepdf = ctx.pikepdf
    parms = stream.get("/DecodeParms")
    if parms is None:
        return False
    candidates = list(parms) if isinstance(parms, pikepdf.Array) else [parms]
    for entry in candidates:
        if isinstance(entry, pikepdf.Dictionary):
            predictor = entry.get("/Predictor")
            try:
                if predictor is not None and int(predictor) > 1:
                    return True
            except Exception:
                return True
    return False


def _unsupported_filter():
    from platform.exceptions import DocumentDisarmFailedException

    return DocumentDisarmFailedException(
        "This PDF uses a compression this server will not decode. Please export a "
        "new PDF and try again."
    )


def _too_big():
    """Also COST, not compatibility — see `ResourceExhausted`."""
    from platform.exceptions import DocumentDisarmFailedException

    return _tagged(DocumentDisarmFailedException(
        "This PDF is too large to process safely. Please upload a shorter CV."
    ), "stream decode budget")


def _inflate_capped(data: bytes, limit: int) -> bytes:
    """zlib inflate that stops at `limit` bytes instead of at the bomb's size."""
    for wbits in (15, -15):
        engine = zlib.decompressobj(wbits)
        try:
            out = engine.decompress(data, limit + 1)
        except zlib.error:
            continue
        if len(out) > limit:
            raise _too_big()
        return out
    raise _unsupported_filter()


def _ascii_hex_decode(data: bytes) -> bytes:
    body = data.split(b">")[0]
    cleaned = bytes(c for c in body if not chr(c).isspace())
    if len(cleaned) % 2:
        cleaned += b"0"
    try:
        return binascii.unhexlify(cleaned)
    except binascii.Error as exc:
        raise _unsupported_filter() from exc


def _ascii85_decode(data: bytes) -> bytes:
    body = data.split(b"~>")[0]
    if body.startswith(b"<~"):
        body = body[2:]
    try:
        return binascii.a2b_base85(body, adobe=False, ignorechars=b" \t\n\r\v\f")
    except Exception as exc:
        raise _unsupported_filter() from exc


def _run_length_decode(data: bytes, limit: int) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        length = data[index]
        index += 1
        if length == 128:
            break
        if length < 128:
            out += data[index : index + length + 1]
            index += length + 1
        else:
            if index >= len(data):
                break
            out += bytes([data[index]]) * (257 - length)
            index += 1
        if len(out) > limit:
            raise _too_big()
    return bytes(out)


# ---------------------------------------------------------------------------
# The filtered copiers
# ---------------------------------------------------------------------------


def _typed(ctx: _Ctx, obj, handler, depth: int):
    """Copy `obj` through `handler`, memoised, bounded, indirect-preserving."""
    if obj is None or depth > _MAX_DEPTH:
        return None
    ctx.budget.tick()
    indirect = bool(getattr(obj, "is_indirect", False))
    memo_key = None
    if indirect:
        memo_key = (obj.objgen, handler.__name__)
        if memo_key in ctx.seen:
            return ctx.seen[memo_key]
    result = handler(ctx, obj, depth)
    if result is None:
        return None
    if indirect:
        result = ctx.out.make_indirect(result)
        ctx.seen[memo_key] = result
    return result


def _plain(ctx: _Ctx, obj, depth: int):
    """A value carried for its own sake: names, strings, numbers, arrays.

    A Dictionary or Stream reaching here is DROPPED rather than copied — the
    point of the allowlist is that a structure nobody named does not travel.
    Names and strings are rebuilt rather than assigned: a `pikepdf` scalar read
    out of the source file belongs to the source `Pdf`, and assigning one into
    the output raises `ForeignObjectError`. That is not a theoretical tidiness
    point — it is the bug that made this module refuse every PDF containing a
    Form XObject, which is to say every CV with a vector logo on it.
    """
    pikepdf = ctx.pikepdf
    if obj is None or depth > _MAX_DEPTH:
        return None
    if isinstance(obj, (pikepdf.Stream, pikepdf.Dictionary)):
        return None
    if isinstance(obj, pikepdf.Array):
        ctx.budget.tick()
        items = [_plain(ctx, value, depth + 1) for value in obj]
        return pikepdf.Array([item for item in items if item is not None])
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    if isinstance(obj, pikepdf.String):
        return pikepdf.String(bytes(obj))
    # Numbers, booleans and null arrive as Python natives, which own nothing:
    return obj


def _dict_of(ctx: _Ctx, obj, keys, handlers, depth: int):
    """A new dictionary holding only `keys`, each through its own copier."""
    pikepdf = ctx.pikepdf
    rebuilt = pikepdf.Dictionary()
    for key in keys:
        if key not in obj:
            continue
        handler = handlers.get(key, _plain)
        value = _typed(ctx, obj[key], handler, depth + 1) if handler is not _plain else _plain(
            ctx, obj[key], depth + 1
        )
        if value is not None:
            rebuilt[key] = value
    return rebuilt


def _map_of(ctx: _Ctx, obj, handler, depth: int):
    """A resource sub-dictionary (name -> object), each value filtered."""
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    rebuilt = pikepdf.Dictionary()
    for name, value in obj.items():
        copied = _typed(ctx, value, handler, depth + 1)
        if copied is not None:
            rebuilt[name] = copied
    return rebuilt


def _copy_resources(ctx: _Ctx, resources, depth: int):
    pikepdf = ctx.pikepdf
    rebuilt = pikepdf.Dictionary()
    if resources is None or not isinstance(resources, pikepdf.Dictionary):
        return rebuilt
    if depth > _MAX_DEPTH:
        return rebuilt
    per_key = {
        "/Font": _copy_font,
        "/XObject": _copy_xobject,
        "/ExtGState": _copy_extgstate,
        "/ColorSpace": _copy_colorspace,
        "/Pattern": _copy_pattern,
        "/Shading": _copy_shading,
    }
    for key in _RESOURCE_KEYS:
        if key not in resources:
            continue
        if key == "/ProcSet":
            value = _plain(ctx, resources[key], depth + 1)
        else:
            value = _map_of(ctx, resources[key], per_key[key], depth + 1)
        if value is not None:
            rebuilt[key] = value
    return rebuilt


def _stream_from_bytes(ctx: _Ctx, source, payload: bytes, keys, handlers, depth: int):
    """A new stream carrying `payload`, plus only the named dictionary keys."""
    stream = ctx.out.make_stream(payload)
    for key, value in _dict_of(ctx, source, keys, handlers, depth).items():
        stream[key] = value
    return stream


def _copy_xobject(ctx: _Ctx, obj, depth: int):
    """Forms are rebuilt like miniature pages; images pass their samples through."""
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Stream):
        return None
    subtype = obj.get("/Subtype")
    if subtype == pikepdf.Name.Form:
        return _copy_form(ctx, obj, depth)
    if subtype == pikepdf.Name.Image:
        return _copy_image(ctx, obj, depth)
    return None


def _copy_form(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if depth > _MAX_FORM_DEPTH * 3:
        return None
    payload = _decoded_stream_bytes(ctx, obj) or b""
    stream = _stream_from_bytes(
        ctx, obj, payload, _FORM_KEYS, {"/Group": _copy_group}, depth
    )
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Form
    stream.Resources = _copy_resources(ctx, obj.get("/Resources"), depth + 1)
    return stream


def _copy_group(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, _GROUP_KEYS, {"/CS": _copy_colorspace}, depth)


def _copy_image(ctx: _Ctx, obj, depth: int):
    """Image samples travel RAW, under a filter list we recognise.

    Decoding them is not an option — DCTDecode and JPXDecode are the viewer's
    business, and re-encoding would destroy the image — so the bytes are copied
    as-is. What does NOT travel is the surrounding dictionary: only the keys
    below, so an /OPI external reference, an /Alternates substitution or a
    stowaway action never comes with it.
    """
    pikepdf = ctx.pikepdf
    filters = _filter_names(ctx, obj)
    if filters is None:
        return None
    raw = bytes(obj.read_raw_bytes())
    ctx.budget.spend_raw(len(raw))
    stream = _stream_from_bytes(
        ctx,
        obj,
        raw,
        _IMAGE_KEYS,
        {"/ColorSpace": _copy_colorspace, "/SMask": _copy_xobject, "/Mask": _copy_xobject},
        depth,
    )
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Image
    # The payload was never decoded, so its filter chain has to travel with it.
    # This is the one place a source filter list survives, and it is why images
    # are named in DOCS.md as residual surface:
    if filters:
        stream.Filter = pikepdf.Array([pikepdf.Name(name) for name in filters])
        parms = obj.get("/DecodeParms")
        if parms is not None:
            copied = _copy_decode_parms(ctx, parms, depth + 1)
            if copied is not None:
                stream.DecodeParms = copied
    else:
        del stream.Filter
    return stream


def _copy_decode_parms(ctx: _Ctx, parms, depth: int):
    """Decode parameters, as scalars only — never a nested object graph."""
    pikepdf = ctx.pikepdf
    if isinstance(parms, pikepdf.Array):
        return pikepdf.Array(
            [_copy_decode_parms(ctx, entry, depth + 1) or pikepdf.Dictionary() for entry in parms]
        )
    if isinstance(parms, pikepdf.Dictionary):
        rebuilt = pikepdf.Dictionary()
        for key, value in parms.items():
            copied = _plain(ctx, value, depth + 1)
            if copied is not None:
                rebuilt[key] = copied
        return rebuilt
    return None


def _copy_font(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    handlers = {
        "/FontDescriptor": _copy_font_descriptor,
        "/DescendantFonts": _copy_descendant_fonts,
        "/Encoding": _copy_encoding,
        "/ToUnicode": _copy_opaque_text_stream,
        "/CIDToGIDMap": _copy_opaque_text_stream,
        "/CharProcs": _copy_char_procs,
    }
    rebuilt = _dict_of(ctx, obj, _FONT_KEYS, handlers, depth)
    # A Type3 font's glyphs are content streams that reference resources of
    # their own, so the font carries a resource dictionary like a page does:
    if "/Resources" in obj:
        rebuilt.Resources = _copy_resources(ctx, obj["/Resources"], depth + 1)
    return rebuilt


def _copy_descendant_fonts(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    entries = list(obj) if isinstance(obj, pikepdf.Array) else [obj]
    copied = [_typed(ctx, entry, _copy_font, depth + 1) for entry in entries]
    return pikepdf.Array([entry for entry in copied if entry is not None])


def _copy_font_descriptor(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    handlers = {
        "/FontFile": _copy_font_file,
        "/FontFile2": _copy_font_file,
        "/FontFile3": _copy_font_file,
        "/CIDSet": _copy_opaque_text_stream,
    }
    return _dict_of(ctx, obj, _FONT_DESCRIPTOR_KEYS, handlers, depth)


def _copy_font_file(ctx: _Ctx, obj, depth: int):
    """The embedded typeface, decoded by us and re-encoded on save."""
    payload = _decoded_stream_bytes(ctx, obj)
    if payload is None:
        return None
    return _stream_from_bytes(
        ctx, obj, payload, ("/Subtype", "/Length1", "/Length2", "/Length3"), {}, depth
    )


def _copy_opaque_text_stream(ctx: _Ctx, obj, depth: int):
    """A CMap or bitmap table: bytes with no dictionary worth keeping."""
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    payload = _decoded_stream_bytes(ctx, obj)
    if payload is None:
        return None
    return ctx.out.make_stream(payload)


def _copy_encoding(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, _ENCODING_KEYS, {}, depth)


def _copy_char_procs(ctx: _Ctx, obj, depth: int):
    """Type3 glyph procedures: each one a content stream in its own right."""
    return _map_of(ctx, obj, _copy_glyph_stream, depth)


def _copy_glyph_stream(ctx: _Ctx, obj, depth: int):
    payload = _decoded_stream_bytes(ctx, obj)
    if payload is None:
        return None
    return ctx.out.make_stream(payload)


def _copy_extgstate(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    handlers = {
        "/SMask": _copy_soft_mask,
        "/Font": _copy_gs_font,
        "/TR": _copy_function,
        "/TR2": _copy_function,
        "/BG": _copy_function,
        "/BG2": _copy_function,
        "/UCR": _copy_function,
        "/UCR2": _copy_function,
        "/HT": _copy_halftone,
        "/HTO": _copy_function,
    }
    return _dict_of(ctx, obj, _EXTGSTATE_KEYS, handlers, depth)


def _copy_gs_font(ctx: _Ctx, obj, depth: int):
    """The ExtGState /Font entry: [font-dictionary, size]."""
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Array) or len(obj) != 2:
        return None
    font = _typed(ctx, obj[0], _copy_font, depth + 1)
    if font is None:
        return None
    return pikepdf.Array([font, _plain(ctx, obj[1], depth + 1)])


def _copy_soft_mask(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    # /G is a Form XObject — the one place a soft mask reaches back into
    # arbitrary content, and the route the first version of this module let
    # through unfiltered:
    return _dict_of(ctx, obj, _SOFT_MASK_KEYS, {"/G": _copy_form, "/TR": _copy_function}, depth)


def _copy_pattern(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    pattern_type = obj.get("/PatternType") if hasattr(obj, "get") else None
    try:
        kind = int(pattern_type) if pattern_type is not None else None
    except Exception:
        return None
    if kind == 1 and isinstance(obj, pikepdf.Stream):
        # A tiling pattern is a content stream with its own resources — same
        # shape as a Form, same treatment:
        payload = _decoded_stream_bytes(ctx, obj) or b""
        stream = _stream_from_bytes(
            ctx,
            obj,
            payload,
            tuple(key for key in _TILING_PATTERN_KEYS if key != "/Resources"),
            {},
            depth,
        )
        stream.Resources = _copy_resources(ctx, obj.get("/Resources"), depth + 1)
        return stream
    if kind == 2 and isinstance(obj, pikepdf.Dictionary):
        return _dict_of(
            ctx,
            obj,
            _SHADING_PATTERN_KEYS,
            {"/Shading": _copy_shading, "/ExtGState": _copy_extgstate},
            depth,
        )
    return None


def _copy_shading(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    handlers = {"/ColorSpace": _copy_colorspace, "/Function": _copy_function}
    if isinstance(obj, pikepdf.Stream):
        # Shading types 4-7 carry their mesh as encoded sample data. It is not
        # a program, so it travels raw under its own filter, like image samples:
        filters = _filter_names(ctx, obj)
        if filters is None:
            return None
        raw = bytes(obj.read_raw_bytes())
        ctx.budget.spend_raw(len(raw))
        stream = _stream_from_bytes(ctx, obj, raw, _SHADING_KEYS, handlers, depth)
        if filters:
            stream.Filter = pikepdf.Array([pikepdf.Name(name) for name in filters])
            parms = obj.get("/DecodeParms")
            if parms is not None:
                copied = _copy_decode_parms(ctx, parms, depth + 1)
                if copied is not None:
                    stream.DecodeParms = copied
        else:
            del stream.Filter
        return stream
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, _SHADING_KEYS, handlers, depth)


def _copy_function(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    if isinstance(obj, pikepdf.Array):
        copied = [_typed(ctx, entry, _copy_function, depth + 1) for entry in obj]
        return pikepdf.Array([entry for entry in copied if entry is not None])
    handlers = {"/Functions": _copy_function}
    if isinstance(obj, pikepdf.Stream):
        # A type-4 PostScript calculator function. It is a program, but a
        # sandboxed arithmetic one with no I/O in its grammar — and it is
        # decoded here, so the viewer never runs an attacker-chosen filter:
        payload = _decoded_stream_bytes(ctx, obj)
        if payload is None:
            return None
        return _stream_from_bytes(ctx, obj, payload, _FUNCTION_KEYS, handlers, depth)
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, _FUNCTION_KEYS, handlers, depth)


def _copy_halftone(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    handlers = {"/TransferFunction": _copy_function, "/SpotFunction": _copy_function}
    if isinstance(obj, pikepdf.Stream):
        payload = _decoded_stream_bytes(ctx, obj)
        if payload is None:
            return None
        return _stream_from_bytes(ctx, obj, payload, _HALFTONE_KEYS, handlers, depth)
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, _HALFTONE_KEYS, handlers, depth)


def _copy_colorspace(ctx: _Ctx, obj, depth: int):
    """A colour space: a bare name, or an array whose FAMILY fixes its shape.

    The first draft copied array members by shape instead — a stream must be an
    ICC profile or an /Indexed lookup, a dictionary must be a tint transform —
    on the reasoning that reading the leading name would mean trusting it. That
    was backwards, and an adversarial diff caught it: shape says nothing about
    ARITY, so a `(tacked on)` string appended to `[/ICCBased 5 0 R]` travelled
    straight into the output and broke the byte-identity property by twelve
    bytes. The family name is not trusted for safety here; it selects a fixed
    member count and a fixed role per position, and anything past that count is
    not copied at all.
    """
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    if not isinstance(obj, pikepdf.Array) or len(obj) < 1:
        return None
    family = obj[0]
    if not isinstance(family, pikepdf.Name):
        return None
    name = str(family)
    members = _COLORSPACE_FAMILIES.get(name)
    if members is None:
        # A device space written the long way, or a family this module does not
        # know. Keep the name alone rather than guessing at its operands:
        return pikepdf.Array([pikepdf.Name(name)])
    copied = [pikepdf.Name(name)]
    for index, role in enumerate(members, start=1):
        if index >= len(obj):
            break
        value = role(ctx, obj[index], depth + 1)
        if value is None:
            return None
        copied.append(value)
    return pikepdf.Array(copied)


def _cs_profile(ctx: _Ctx, obj, depth: int):
    """An ICC profile stream: opaque bytes plus the two keys that describe it."""
    return _typed(ctx, obj, _cs_profile_stream, depth)


def _cs_profile_stream(ctx: _Ctx, obj, depth: int):
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Stream):
        return None
    filters = _filter_names(ctx, obj)
    if filters is None:
        return None
    raw = bytes(obj.read_raw_bytes())
    ctx.budget.spend_raw(len(raw))
    stream = _stream_from_bytes(ctx, obj, raw, _COLORSPACE_STREAM_KEYS, {}, depth)
    if filters:
        stream.Filter = pikepdf.Array([pikepdf.Name(entry) for entry in filters])
    else:
        del stream.Filter
    return stream


def _cs_space(ctx: _Ctx, obj, depth: int):
    """A nested colour space (the base of /Indexed, the alternate of /Separation)."""
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.Name):
        return pikepdf.Name(str(obj))
    return _typed(ctx, obj, _copy_colorspace, depth)


def _cs_lookup(ctx: _Ctx, obj, depth: int):
    """An /Indexed palette: a byte string, or a stream holding the same bytes."""
    pikepdf = ctx.pikepdf
    if isinstance(obj, pikepdf.String):
        return pikepdf.String(bytes(obj))
    payload = _decoded_stream_bytes(ctx, obj)
    if payload is None:
        return None
    return ctx.out.make_stream(payload)


def _cs_transform(ctx: _Ctx, obj, depth: int):
    return _typed(ctx, obj, _copy_function, depth)


def _cs_params(ctx: _Ctx, obj, depth: int):
    """The parameter dictionary of a CIE-based space: numbers and arrays only."""
    pikepdf = ctx.pikepdf
    if not isinstance(obj, pikepdf.Dictionary):
        return None
    return _dict_of(ctx, obj, ("/WhitePoint", "/BlackPoint", "/Gamma", "/Matrix", "/Range"), {}, depth)


# Family -> the role of each member AFTER the family name. The length of the
# tuple is the arity: a member past it is never read. /DeviceN's optional fifth
# member (an attributes dictionary of rendering hints) is deliberately absent —
# it is optional by spec, it reaches further nested spaces, and nothing renders
# differently without it.
_COLORSPACE_FAMILIES = {
    "/ICCBased": (_cs_profile,),
    "/Indexed": (_cs_space, _plain, _cs_lookup),
    "/I": (_cs_space, _plain, _cs_lookup),
    "/Separation": (_plain, _cs_space, _cs_transform),
    "/DeviceN": (_plain, _cs_space, _cs_transform),
    "/CalRGB": (_cs_params,),
    "/CalGray": (_cs_params,),
    "/Lab": (_cs_params,),
    "/Pattern": (_cs_space,),
    "/DeviceGray": (),
    "/DeviceRGB": (),
    "/DeviceCMYK": (),
    "/G": (),
    "/RGB": (),
    "/CMYK": (),
}


# --- Seams used by the ladder's pre-raster cost probe -----------------------
# Public because `disarm.py` has to answer "what does this cost?" independently
# of "can I rebuild it?", and the answer needs the same capped decoder this
# module already uses rather than a second, subtly different one.
FLATE = _FLATE


def filter_names(stream) -> Optional[list[str]]:
    """The stream's filter chain as plain strings, or None if unreadable."""
    import pikepdf

    class _Shim:
        pikepdf = None

    shim = _Shim()
    shim.pikepdf = pikepdf
    return _filter_names(shim, stream)


def inflate_capped(raw: bytes, limit: int) -> bytes:
    """Capped inflate, so a bomb is measured rather than paid for."""
    return _inflate_capped(raw, limit)


def cost_refusal(message: str):
    """A refusal tagged as COST, for a caller that measured rather than tried."""
    from platform.exceptions import DocumentDisarmFailedException

    return _tagged(DocumentDisarmFailedException(message), message)
