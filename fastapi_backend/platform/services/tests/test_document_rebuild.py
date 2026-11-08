"""The allowlist rebuild's safety property, asserted rather than argued.

The claim is not "we strip the dangerous keys" — a denylist has to enumerate
every hiding place, and PDF has more than anyone enumerates correctly (this
repo's own `document_sanitize` misses inline actions under unenumerated keys).
The claim here is mechanical:

    rebuild(clean) == rebuild(clean + attack)    byte for byte

If that holds, the attack contributed nothing, because the rebuilder never read
the region it lives in. These tests are the check.
"""

import hashlib
import io
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest

from platform.exceptions import DocumentDisarmFailedException
from platform.services.document_ingest.rebuild import rebuild_pdf_preserving_text


def _cv(pages: int = 1) -> pikepdf.Pdf:
    """A CV with real vector text — the thing that must survive."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    for n in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
        page = pdf.pages[n]
        page.Contents = pdf.make_stream(
            f"BT /F1 18 Tf 60 760 Td (Priya Raman) Tj ET\n"
            f"BT /F1 10 Tf 60 740 Td (priya.raman@example.ac.uk) Tj ET".encode())
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    return pdf


def _bytes(pdf: pikepdf.Pdf, trailing: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue() + trailing


def _text(data: bytes) -> str:
    document = pdfium.PdfDocument(io.BytesIO(data))
    try:
        return document[0].get_textpage().get_text_range()
    finally:
        document.close()


# Each entry mutates a clean CV into a hostile one. The rebuild must not notice.
def _open_action_js(pdf):
    pdf.Root.OpenAction = pdf.make_indirect(
        pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="app.alert('pwn')"))

def _names_javascript_tree(pdf):
    pdf.Root.Names = pdf.make_indirect(pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(
        Names=pikepdf.Array(["e", pdf.make_indirect(
            pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="app.launchURL('http://evil')"))]))))

def _launch_annotation(pdf):
    pdf.pages[0].Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=pikepdf.Array([0, 0, 99, 99]),
        A=pikepdf.Dictionary(S=pikepdf.Name.Launch, F="cmd.exe")))])

def _uri_annotation(pdf):
    pdf.pages[0].Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=pikepdf.Array([0, 0, 99, 99]),
        A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI="http://evil.example/steal")))])

def _acroform_xfa(pdf):
    pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(
        XFA=pdf.make_stream(b"<xdp:xdp><script>evil()</script></xdp:xdp>"),
        Fields=pikepdf.Array([])))

def _embedded_executable(pdf):
    payload = pdf.make_stream(b"MZ\x90\x00" + b"\x00" * 256)
    pdf.Root.Names = pdf.make_indirect(pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(
        Names=pikepdf.Array(["payload.exe", pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Filespec, F="payload.exe",
            EF=pikepdf.Dictionary(F=payload)))]))))

def _page_additional_actions(pdf):
    pdf.pages[0].AA = pikepdf.Dictionary(O=pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="evil()"))

def _screen_annotation(pdf):
    pdf.pages[0].Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Screen, Rect=pikepdf.Array([0, 0, 9, 9])))])

def _external_goto(pdf):
    pdf.Root.OpenAction = pdf.make_indirect(
        pikepdf.Dictionary(S=pikepdf.Name.GoToR, F="http://evil.example/next.pdf"))

def _xmp_metadata(pdf):
    pdf.Root.Metadata = pdf.make_stream(b"<?xpacket?><x:xmpmeta>CANARY_XMP</x:xmpmeta>")

def _outlines_with_action(pdf):
    pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Outlines, Count=1,
        First=pdf.make_indirect(pikepdf.Dictionary(
            Title="click me", A=pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="evil()")))))

# The attacks above all live in regions the rebuild never opens, which is the
# easy half of the claim. These live INSIDE regions it must open — a resource a
# content stream genuinely references — so they are only harmless if each
# category is copied through its own key list. The first version of this module
# handed every category except Form XObjects to `copy_foreign`, which copies an
# object and its whole subgraph verbatim, and an adversarial pass proved an
# /OpenAction /JavaScript dictionary parked in an ExtGState reached the output
# intact. Each of these reproduces one of those routes.
def _resources(pdf):
    return pdf.pages[0].obj.Resources


def _payload():
    return pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS="app.alert('evil.example')")


def _extgstate_stowaway(pdf):
    _resources(pdf).ExtGState = pikepdf.Dictionary(GS0=pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.ExtGState, ca=1, OpenAction=_payload(), Stowaway="app.alert(1)")))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        bytes(pdf.pages[0].obj.Contents.read_bytes()) + b"\nq /GS0 gs Q")


def _extgstate_carrier(pdf):
    """The clean twin of the above: the ExtGState is referenced either way, so
    the hash equality is about its CONTENTS rather than about its presence."""
    _resources(pdf).ExtGState = pikepdf.Dictionary(GS0=pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.ExtGState, ca=1)))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        bytes(pdf.pages[0].obj.Contents.read_bytes()) + b"\nq /GS0 gs Q")


def _font_stowaway(pdf):
    _resources(pdf).Font.F1.OpenAction = _payload()
    _resources(pdf).Font.F1.EmbeddedFiles = pikepdf.Dictionary(F="cmd.exe")


def _colorspace_extra_member(pdf):
    """A member tacked past a colour space's real arity. Copying array members
    by SHAPE let this through — shape says nothing about how many there are."""
    profile = pdf.make_stream(b"\x00" * 64)
    profile.N = 3
    _resources(pdf).ColorSpace = pikepdf.Dictionary(
        CS0=pikepdf.Array([pikepdf.Name.ICCBased, pdf.make_indirect(profile), "app.alert(1)"]))


def _colorspace_carrier(pdf):
    profile = pdf.make_stream(b"\x00" * 64)
    profile.N = 3
    _resources(pdf).ColorSpace = pikepdf.Dictionary(
        CS0=pikepdf.Array([pikepdf.Name.ICCBased, pdf.make_indirect(profile)]))


def _pattern_stowaway(pdf):
    tile = pdf.make_stream(b"1 0 0 rg 0 0 5 5 re f")
    tile.Type, tile.PatternType, tile.PaintType, tile.TilingType = pikepdf.Name.Pattern, 1, 1, 1
    tile.BBox, tile.XStep, tile.YStep = pikepdf.Array([0, 0, 10, 10]), 10, 10
    tile.Resources = pikepdf.Dictionary()
    tile.AA = pikepdf.Dictionary(O=_payload())
    _resources(pdf).Pattern = pikepdf.Dictionary(P0=pdf.make_indirect(tile))


def _pattern_carrier(pdf):
    tile = pdf.make_stream(b"1 0 0 rg 0 0 5 5 re f")
    tile.Type, tile.PatternType, tile.PaintType, tile.TilingType = pikepdf.Name.Pattern, 1, 1, 1
    tile.BBox, tile.XStep, tile.YStep = pikepdf.Array([0, 0, 10, 10]), 10, 10
    tile.Resources = pikepdf.Dictionary()
    _resources(pdf).Pattern = pikepdf.Dictionary(P0=pdf.make_indirect(tile))


def _soft_mask_form_stowaway(pdf):
    """A Form reached through /ExtGState/SMask/G rather than through
    /Resources/XObject — the same object, a route the first version's
    Form-only special case never covered."""
    mask = pdf.make_stream(b"0.5 g 0 0 40 40 re f")
    mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    mask.BBox = pikepdf.Array([0, 0, 40, 40])
    mask.Ref = pikepdf.Dictionary(F="http://evil.example/x.pdf", Page=0)
    _resources(pdf).ExtGState = pikepdf.Dictionary(GS0=pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.ExtGState,
        SMask=pikepdf.Dictionary(Type=pikepdf.Name.Mask, S=pikepdf.Name.Luminosity,
                                 G=pdf.make_indirect(mask)))))


def _soft_mask_form_carrier(pdf):
    mask = pdf.make_stream(b"0.5 g 0 0 40 40 re f")
    mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    mask.BBox = pikepdf.Array([0, 0, 40, 40])
    _resources(pdf).ExtGState = pikepdf.Dictionary(GS0=pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.ExtGState,
        SMask=pikepdf.Dictionary(Type=pikepdf.Name.Mask, S=pikepdf.Name.Luminosity,
                                 G=pdf.make_indirect(mask)))))


def _image_stowaway(pdf):
    image = pdf.make_stream(bytes([200, 30, 60] * 16))
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height, image.BitsPerComponent = 4, 4, 8
    image.ColorSpace = pikepdf.Name.DeviceRGB
    image.Alternates = pikepdf.Array([pikepdf.Dictionary(Image="app.alert(1)")])
    image.OPI = pikepdf.Dictionary(F="http://evil.example/spread.tif")
    _resources(pdf).XObject = pikepdf.Dictionary(Im0=pdf.make_indirect(image))


def _image_carrier(pdf):
    image = pdf.make_stream(bytes([200, 30, 60] * 16))
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height, image.BitsPerComponent = 4, 4, 8
    image.ColorSpace = pikepdf.Name.DeviceRGB
    _resources(pdf).XObject = pikepdf.Dictionary(Im0=pdf.make_indirect(image))


# hostile -> the clean twin it must be byte-identical to. A carrier of None
# means the plain CV: the attack adds a structure nothing references.
RESOURCE_ATTACKS = {
    "javascript inside an ExtGState": (_extgstate_stowaway, _extgstate_carrier),
    "action dictionary inside a font": (_font_stowaway, None),
    "extra member past a colour space's arity": (_colorspace_extra_member, _colorspace_carrier),
    "additional actions on a tiling pattern": (_pattern_stowaway, _pattern_carrier),
    "external reference on a soft-mask form": (_soft_mask_form_stowaway, _soft_mask_form_carrier),
    "alternate image and OPI link on an image": (_image_stowaway, _image_carrier),
}


ATTACKS = {
    "open-action javascript": _open_action_js,
    "names javascript tree": _names_javascript_tree,
    "launch annotation": _launch_annotation,
    "uri annotation": _uri_annotation,
    "acroform + xfa": _acroform_xfa,
    "embedded executable": _embedded_executable,
    "page additional actions": _page_additional_actions,
    "screen annotation": _screen_annotation,
    "external goto": _external_goto,
    "xmp metadata canary": _xmp_metadata,
    "outline with action": _outlines_with_action,
}


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_an_attack_contributes_nothing_to_the_output(name):
    """THE property. Not 'the payload was removed' — 'the payload was never
    read', which is what makes this robust against the attack nobody listed."""
    clean = rebuild_pdf_preserving_text(_bytes(_cv()))
    hostile_source = _cv()
    ATTACKS[name](hostile_source)
    hostile = rebuild_pdf_preserving_text(_bytes(hostile_source))

    assert hashlib.sha256(hostile).hexdigest() == hashlib.sha256(clean).hexdigest(), (
        f"the {name!r} attack changed the rebuilt output — something in the "
        "rebuild is reading a region attacks live in"
    )


def test_trailing_polyglot_bytes_contribute_nothing():
    """Appended HTML/ZIP after %%EOF — the polyglot shape."""
    clean = rebuild_pdf_preserving_text(_bytes(_cv()))
    polyglot = rebuild_pdf_preserving_text(
        _bytes(_cv(), trailing=b"\n<html><script>alert(1)</script></html>"))
    assert polyglot == clean


def test_the_text_layer_survives():
    """The entire point of preferring this over rasterising: a CV stays
    searchable, selectable, screen-reader-readable and ATS-parseable."""
    source = _bytes(_cv())
    rebuilt = rebuild_pdf_preserving_text(source)
    assert "Priya Raman" in _text(rebuilt)
    assert "priya.raman@example.ac.uk" in _text(rebuilt)


def test_output_is_a_pure_function_of_the_input():
    """Same CV in, same bytes out — so the stored sha256 is stable, and so the
    equality the tests above rely on is exact rather than same-second luck."""
    source = _bytes(_cv())
    assert rebuild_pdf_preserving_text(source) == rebuild_pdf_preserving_text(source)


def test_no_attacker_payload_appears_anywhere_in_the_bytes():
    """Belt and braces beside the hash equality: grep the raw output."""
    hostile = _cv()
    for apply in ATTACKS.values():
        apply(hostile)
    rebuilt = rebuild_pdf_preserving_text(_bytes(hostile))
    for marker in (b"app.alert", b"cmd.exe", b"evil.example", b"CANARY_XMP", b"MZ\x90",
                   b"<script>", b"/JavaScript", b"/Launch", b"/XFA", b"/EmbeddedFile",
                   b"/AA", b"/URI", b"/Annots", b"/OpenAction"):
        assert marker not in rebuilt, f"{marker!r} survived into the safe copy"


def test_a_document_with_no_pages_is_refused():
    empty = pikepdf.new()
    with pytest.raises(DocumentDisarmFailedException):
        rebuild_pdf_preserving_text(_bytes(empty))


def test_a_non_pdf_is_refused():
    with pytest.raises(DocumentDisarmFailedException):
        rebuild_pdf_preserving_text(b"not a pdf at all")


def test_resources_inherited_from_the_pages_node_are_not_lost():
    """/Resources and /MediaBox are inheritable, and Word and LaTeX both hoist
    shared fonts onto the parent node. A page whose fonts resolved to nothing
    would rebuild blank while its content stream still named them.

    Honest scope: qpdf pushes inherited attributes down to the leaf on open, so
    this passes with or without the rebuild's own /Parent walk — it pins the
    OBSERVABLE behaviour end to end, and does not guard that helper. Kept
    because the behaviour is what matters to a member whose CV must not come
    back blank, whoever is responsible for delivering it.
    """
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    pdf.add_blank_page(page_size=(595, 842))
    page = pdf.pages[0]
    page.Contents = pdf.make_stream(b"BT /F1 18 Tf 60 700 Td (Inherited Priya) Tj ET")
    # Hoist to the parent and strip from the leaf — legal, and common:
    pdf.Root.Pages.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    pdf.Root.Pages.MediaBox = pikepdf.Array([0, 0, 595, 842])
    for key in ("/Resources", "/MediaBox"):
        if key in page.obj:
            del page.obj[key]

    rebuilt = rebuild_pdf_preserving_text(_bytes(pdf))

    assert "Inherited Priya" in _text(rebuilt)
    with pikepdf.open(io.BytesIO(rebuilt)) as out:
        fonts = out.pages[0].obj["/Resources"]["/Font"]
        assert "/F1" in fonts, "the inherited font did not reach the rebuilt page"
        # Inherited geometry too — otherwise the page silently becomes Letter:
        assert [float(v) for v in out.pages[0].obj["/MediaBox"]] == [0, 0, 595, 842]


@pytest.mark.parametrize("name", sorted(RESOURCE_ATTACKS))
def test_a_payload_inside_a_referenced_resource_contributes_nothing(name):
    """The same property as above, but for the half of the document the rebuild
    genuinely has to read. A resource is copied through its category's own key
    list, so a key nobody named does not travel with it — which is what makes
    the hostile file and its clean twin identical byte for byte."""
    hostile_source, carrier = RESOURCE_ATTACKS[name]

    clean_pdf = _cv()
    if carrier is not None:
        carrier(clean_pdf)
    clean = rebuild_pdf_preserving_text(_bytes(clean_pdf))

    hostile_pdf = _cv()
    hostile_source(hostile_pdf)
    hostile = rebuild_pdf_preserving_text(_bytes(hostile_pdf))

    assert hashlib.sha256(hostile).hexdigest() == hashlib.sha256(clean).hexdigest(), (
        f"{name!r} changed the rebuilt output — that resource category is being "
        "copied wholesale instead of through its own key list"
    )


def _form_xobject_cv() -> pikepdf.Pdf:
    """A CV with a vector logo: a Form XObject, exactly as any designed CV,
    LaTeX title block or exported-from-Illustrator header carries."""
    pdf = _cv()
    inner = pdf.make_stream(b"0 0 1 rg 0 0 20 20 re f")
    inner.Type, inner.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    inner.BBox = pikepdf.Array([0, 0, 20, 20])
    outer = pdf.make_stream(b"q 1 0 0 1 5 5 cm /Inner Do Q")
    outer.Type, outer.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    outer.BBox = pikepdf.Array([0, 0, 40, 40])
    outer.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Inner=pdf.make_indirect(inner)))
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Logo=pdf.make_indirect(outer))
    pdf.pages[0].obj.Contents = pdf.make_stream(
        bytes(pdf.pages[0].obj.Contents.read_bytes()) + b"\nq 1 0 0 1 60 640 cm /Logo Do Q")
    return pdf


def test_a_cv_with_a_vector_logo_is_not_refused():
    """The regression this exists for: `/Subtype` is MANDATORY on a Form
    XObject, and the first version assigned that name straight from the source
    file into an output-owned stream. pikepdf raises ForeignObjectError on a
    value that still belongs to another Pdf, the rebuild caught it as an
    unknown failure, and every CV carrying a vector logo was refused with
    "could not be processed safely". A plain, entirely benign document."""
    rebuilt = rebuild_pdf_preserving_text(_bytes(_form_xobject_cv()))

    assert "Priya Raman" in _text(rebuilt)
    with pikepdf.open(io.BytesIO(rebuilt)) as out:
        logo = out.pages[0].obj.Resources.XObject.Logo
        assert logo.Subtype == pikepdf.Name.Form
        assert "/Inner" in logo.Resources.XObject, "the nested form did not survive"


def _flate_bomb_cv(decoded_bytes: int) -> bytes:
    """A small upload whose content stream inflates to `decoded_bytes`."""
    pdf = _cv()
    pdf.pages[0].obj.Contents = pdf.make_stream(b"1 0 0 RG\n" * (decoded_bytes // 9))
    buffer = io.BytesIO()
    pdf.save(buffer, compress_streams=True)
    return buffer.getvalue()


def test_a_decompression_bomb_is_refused_rather_than_inflated():
    """The rasterising path has carried memory bounds since a 258-byte upload
    was shown to cost 3.6GB. This path had none, on the theory that not
    rendering made it cheap — but `read_bytes` inflates the whole stream before
    anyone can measure it, and a 120KB file expanding to 60MB was measured at
    511MB RSS. The bound has to live in the rebuild, because the call site's
    concurrency limiter caps how MANY rebuilds run and never what one costs."""
    bomb = _flate_bomb_cv(200 * 1024 * 1024)
    assert len(bomb) < 1024 * 1024, "the bomb must be small on the wire, or it proves nothing"

    with pytest.raises(DocumentDisarmFailedException):
        rebuild_pdf_preserving_text(bomb)


def test_an_ordinary_content_stream_is_not_caught_by_the_bomb_bound():
    """The other side of that bound: a text-heavy CV decodes to a couple of MB
    and must sail through. A guard that refuses real documents is a worse bug
    than the one it fixes."""
    rebuilt = rebuild_pdf_preserving_text(_flate_bomb_cv(2 * 1024 * 1024))
    assert rebuilt.startswith(b"%PDF-")


def test_a_long_academic_cv_keeps_every_page_and_its_text():
    """This rung used to decline at 40 pages, and that cap cost real documents.

    The ladder above rasterises whatever this rung declines, so declining a
    60-page academic CV did not protect anyone: it handed the file to the
    raster rung, which then truncated it to 43 pages under its own pixel
    budget. The member silently lost seventeen pages AND their text layer, to
    satisfy a number that never described what this rung could afford. What it
    can afford is bounded by the byte and object budgets, which is where the
    bound belongs. Measured: 60 pages rebuild to under 10KB here, against 1.7MB
    for the truncated raster."""
    rebuilt = rebuild_pdf_preserving_text(_bytes(_cv(pages=60)))

    with pikepdf.open(io.BytesIO(rebuilt)) as out:
        assert len(out.pages) == 60
    assert "Priya Raman" in _text(rebuilt)
    assert len(rebuilt) < 200_000, "a 60-page text CV must stay small on this rung"


def test_an_undecodable_filter_is_refused_rather_than_guessed():
    """LZW has an unbounded expansion ratio and no capped decoder here. A file
    using it is refused, not decoded through a path with no ceiling.

    Hand-built rather than saved through pikepdf on purpose: qpdf decodes LZW
    and re-encodes it as Flate on save, so a fixture built the convenient way
    would arrive here already converted and would pass whatever this module
    did."""
    raw = b"""%PDF-1.7
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 595 842] >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj
4 0 obj << /Length 4 /Filter /LZWDecode >> stream
\x80\x0b\x60\x50
endstream endobj
trailer << /Root 1 0 R /Size 5 >>
"""

    with pytest.raises(DocumentDisarmFailedException):
        rebuild_pdf_preserving_text(raw)


def test_an_embedded_font_programme_survives():
    """Fonts are the reason this path exists rather than rasterising, and a
    filtered copy that dropped /FontFile2 would render the CV in a substitute
    typeface without failing — silent, and visible only to the reader."""
    # A real upstream font, committed with its license beside these tests.
    # The guard must reach the rebuild on macOS as well as on Linux, without
    # depending on either host's font installation.
    ttf = (Path(__file__).parent / "fixtures" / "DejaVuSans.ttf").read_bytes()
    pdf = _cv()
    font_file = pdf.make_stream(ttf)
    font_file.Length1 = len(ttf)
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/DejaVuSans"), Flags=4,
        FontBBox=pikepdf.Array([-1021, -463, 1793, 1232]), ItalicAngle=0, Ascent=928,
        Descent=-236, CapHeight=700, StemV=80, FontFile2=pdf.make_indirect(font_file))
    descendant = pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name("/DejaVuSans"), DW=1000, CIDToGIDMap=pikepdf.Name.Identity,
        CIDSystemInfo=pikepdf.Dictionary(Registry="Adobe", Ordering="Identity", Supplement=0),
        FontDescriptor=pdf.make_indirect(descriptor))
    pdf.pages[0].obj.Resources.Font.F0 = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0, BaseFont=pikepdf.Name("/DejaVuSans"),
        Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=pikepdf.Array([pdf.make_indirect(descendant)])))

    rebuilt = rebuild_pdf_preserving_text(_bytes(pdf))

    with pikepdf.open(io.BytesIO(rebuilt)) as out:
        embedded = out.pages[0].obj.Resources.Font.F0.DescendantFonts[0].FontDescriptor
        assert "/FontFile2" in embedded, "the embedded typeface was dropped"
        assert len(bytes(embedded.FontFile2.read_bytes())) == len(ttf)
        assert bytes(embedded.FontFile2.read_bytes()) == ttf, "the embedded typeface was altered"


def test_a_source_owned_name_on_a_rebuilt_stream_does_not_break_the_rebuild():
    """pikepdf objects belong to the `Pdf` they were read from, and assigning
    one into a stream owned by the OUTPUT raises ForeignObjectError. That is
    not a tidiness point: it is the shape of the bug that made this module
    refuse every CV with a vector logo, and it fails as a hard refusal rather
    than as a dropped key, so nothing about it is subtle in production and
    nothing about it is visible in a test that never puts a Name there.

    Both of these do: an image's rendering /Intent and a Type1C font
    programme's /Subtype are Names read from the source, written onto streams
    this module created."""
    pdf = _cv()
    image = pdf.make_stream(bytes([200, 30, 60] * 16))
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height, image.BitsPerComponent = 4, 4, 8
    image.ColorSpace = pikepdf.Name.DeviceRGB
    image.Intent = pikepdf.Name.RelativeColorimetric
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=pdf.make_indirect(image))

    font_file = pdf.make_stream(b"%!PS-AdobeFont-1.0\n" + b"\x00" * 64)
    font_file.Subtype = pikepdf.Name.Type1C
    pdf.pages[0].obj.Resources.Font.F1.FontDescriptor = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name.Helvetica, Flags=4,
        FontBBox=pikepdf.Array([0, 0, 1, 1]), ItalicAngle=0, Ascent=1, Descent=0,
        CapHeight=1, StemV=1, FontFile3=pdf.make_indirect(font_file)))

    rebuilt = rebuild_pdf_preserving_text(_bytes(pdf))

    with pikepdf.open(io.BytesIO(rebuilt)) as out:
        resources = out.pages[0].obj.Resources
        assert resources.XObject.Im0.Intent == pikepdf.Name.RelativeColorimetric
        assert resources.Font.F1.FontDescriptor.FontFile3.Subtype == pikepdf.Name.Type1C
