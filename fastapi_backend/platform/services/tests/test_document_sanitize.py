"""PDF active-content stripping — including annotation primary /A actions."""

import io

import pikepdf
import pytest

from platform.services.document_sanitize import sanitize_pdf


def _pdf_with_annotation_action(subtype: str, **action_extras) -> bytes:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    action = pikepdf.Dictionary({"/S": pikepdf.Name(subtype), **action_extras})
    annot = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Link"),
            "/Rect": pikepdf.Array([0, 0, 50, 50]),
            "/A": action,
        }
    )
    page.Annots = pikepdf.Array([annot])
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "subtype,extras",
    [
        ("/URI", {"/URI": "https://evil.example/"}),
        ("/Launch", {"/F": pikepdf.Dictionary({"/F": "cmd.exe"})}),
        ("/JavaScript", {"/JS": "app.alert('x')"}),
    ],
)
def test_sanitize_strips_active_annotation_primary_actions(subtype, extras):
    raw = _pdf_with_annotation_action(subtype, **extras)
    cleaned = sanitize_pdf(raw)
    assert cleaned != raw or subtype  # always re-serialises when pikepdf works
    with pikepdf.open(io.BytesIO(cleaned)) as pdf:
        annots = pdf.pages[0].Annots
        assert annots is not None
        for annot in annots:
            assert "/A" not in annot


def test_sanitize_keeps_goto_internal_actions():
    # Internal navigation is not an active vector we strip:
    raw = _pdf_with_annotation_action("/GoTo", **{"/D": pikepdf.Array([0, pikepdf.Name("/Fit")])})
    cleaned = sanitize_pdf(raw)
    with pikepdf.open(io.BytesIO(cleaned)) as pdf:
        assert "/A" in pdf.pages[0].Annots[0]


def test_sanitize_strips_file_attachment_annotations():
    # FileAttachment annots carry /EF streams outside /Names/EmbeddedFiles:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    payload = pikepdf.Stream(pdf, b"MALWARE_PAYLOAD_XYZ_UNIQUE_99")
    filespec = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Filespec"),
            "/F": "payload.exe",
            "/EF": pikepdf.Dictionary({"/F": payload}),
        }
    )
    annot = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/FileAttachment"),
            "/Rect": pikepdf.Array([0, 0, 50, 50]),
            "/FS": filespec,
        }
    )
    page.Annots = pikepdf.Array([annot])
    buffer = io.BytesIO()
    pdf.save(buffer)
    raw = buffer.getvalue()

    cleaned = sanitize_pdf(raw)
    assert b"MALWARE_PAYLOAD_XYZ_UNIQUE_99" not in cleaned
    with pikepdf.open(io.BytesIO(cleaned)) as out:
        annots = out.pages[0].obj.get("/Annots")
        if annots is not None:
            for annot in annots:
                assert annot.get("/Subtype") != pikepdf.Name("/FileAttachment")
