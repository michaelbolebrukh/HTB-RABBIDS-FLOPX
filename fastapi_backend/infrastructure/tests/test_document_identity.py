"""
Deterministic CV name extraction — pure scorer tests plus real-PDF round trips.

The PDF fixtures are hand-built minimal one-page documents (uncompressed
content streams, correct xref) so the pdfminer path is exercised end to end
without binary fixtures in the repo. The scorer is also tested directly with
Unicode names that PDF Latin-1 fixtures cannot carry.
"""

import pytest

from platform.services.document_identity import (
    TextLine,
    extract_email_from_pdf,
    extract_name_from_pdf,
    pick_email,
    pick_name,
)


# ============================================================================
# MINIMAL PDF BUILDER
# ============================================================================

def _build_pdf(lines: list[tuple[str, float]], author: str | None = None) -> bytes:
    """A valid one-page PDF with each (text, font_size) as its own line, top-down."""
    content_parts = []
    y = 760.0
    for text, size in lines:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_parts.append(f"BT /F1 {size:g} Tf 72 {y:g} Td ({escaped}) Tj ET")
        y -= size * 1.8
    stream = "\n".join(content_parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if author is not None:
        objects.append(b"<< /Author (" + author.encode("latin-1") + b") >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_position = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {size} /Root 1 0 R"
    if author is not None:
        trailer += f" /Info {len(objects)} 0 R"
    trailer += f" >>\nstartxref\n{xref_position}\n%%EOF"
    out += trailer.encode()
    return bytes(out)


TYPICAL_CV = [
    ("Jane Doe", 24.0),
    ("jane.doe@example.com | 07123 456789", 10.0),
    ("Education", 14.0),
    ("Imperial College London", 11.0),
    ("BSc Mathematics, First Class", 11.0),
    ("Experience", 14.0),
    ("Quantitative Research Intern", 11.0),
]


# ============================================================================
# END-TO-END (REAL PDF)
# ============================================================================

def test_extracts_largest_font_header_name():
    assert extract_name_from_pdf(_build_pdf(TYPICAL_CV)) == "Jane Doe"


def test_extracts_email_from_typical_cv():
    assert extract_email_from_pdf(_build_pdf(TYPICAL_CV)) == "jane.doe@example.com"


def test_extract_email_malformed_pdf_returns_none():
    assert extract_email_from_pdf(b"%PDF-1.4 not really a pdf") is None
    assert extract_email_from_pdf(b"") is None


def test_all_caps_name_is_title_cased():
    lines = [("JANE DOE", 24.0)] + TYPICAL_CV[1:]
    assert extract_name_from_pdf(_build_pdf(lines)) == "Jane Doe"


def test_email_anchor_beats_a_bigger_job_title():
    # The name is small and below a large non-name line; only the account email
    # identifies it:
    lines = [
        ("Quantitative Analyst", 26.0),
        ("Jane Doe", 11.0),
        ("Building pricing models since 2021", 11.0),
    ]
    result = extract_name_from_pdf(_build_pdf(lines), email="jane.doe@example.com")
    assert result == "Jane Doe"


def test_author_metadata_corroborates_but_never_wins_alone():
    # /Author matching a text candidate boosts it; an /Author with no matching
    # name-shaped line on the page cannot produce a result by itself:
    lines = [("Skills and Experience", 18.0), ("Pricing Models Overview", 12.0)]
    assert extract_name_from_pdf(_build_pdf(lines, author="Jane Doe")) is None

    # Two equally-shaped candidates at the same size: /Author picks the right one
    # even though the other line comes first:
    with_name = [("Jane Doe", 12.0), ("Alex Martin", 12.0)]
    result = extract_name_from_pdf(_build_pdf(with_name, author="Alex Martin"))
    assert result == "Alex Martin"


def test_no_name_shaped_lines_returns_none():
    lines = [
        ("Curriculum Vitae", 24.0),
        ("Education", 14.0),
        ("Experience", 14.0),
    ]
    assert extract_name_from_pdf(_build_pdf(lines)) is None


def test_malformed_pdf_returns_none():
    assert extract_name_from_pdf(b"%PDF-1.4 not really a pdf") is None
    assert extract_name_from_pdf(b"") is None


def test_determinism_same_bytes_same_answer():
    data = _build_pdf(TYPICAL_CV)
    results = {extract_name_from_pdf(data, email="jane.doe@example.com") for _ in range(5)}
    assert results == {"Jane Doe"}


# ============================================================================
# PURE SCORER (Unicode and shape rules without PDF encoding limits)
# ============================================================================

def _line(text: str, size: float, order: int) -> TextLine:
    return TextLine(text=text, font_size=size, order=order)


def test_diacritics_and_particles_are_name_shaped():
    lines = [_line("José García-Márquez", 24.0, 0), _line("Education", 12.0, 1)]
    assert pick_name(lines) == "José García-Márquez"


def test_apostrophe_names_are_name_shaped():
    lines = [_line("Siobhán O'Brien", 22.0, 0), _line("Experience", 12.0, 1)]
    assert pick_name(lines) == "Siobhán O'Brien"


@pytest.mark.parametrize("rejected", [
    "Curriculum Vitae",
    "Imperial College London",
    "MSc Financial Mathematics",
    "jane.doe@example.com",
    "07123 456789",
    "London, United Kingdom",
    "linkedin.com/in/janedoe",
    "J",  # Too short
    "A" * 41 + " B",  # Too long
    "Jane",  # One token is never enough
    "One Two Three Four Five",  # Five tokens is a sentence, not a name
])
def test_rejected_shapes(rejected):
    lines = [_line(rejected, 24.0, 0), _line("Other Bigger Context", 30.0, 1)]
    result = pick_name(lines)
    assert result != rejected.strip()


def test_initial_plus_surname_email_matches():
    lines = [
        _line("Something Else Entirely", 26.0, 0),
        _line("Jane Doe", 11.0, 1),
        _line("More Body Text", 11.0, 2),
    ]
    assert pick_name(lines, email="jdoe@example.com") == "Jane Doe"


def test_concatenated_email_local_part_matches():
    lines = [
        _line("Something Else Entirely", 26.0, 0),
        _line("Jane Doe", 11.0, 1),
    ]
    assert pick_name(lines, email="janedoe04@example.com") == "Jane Doe"


def test_weak_deep_candidate_falls_below_threshold():
    # A small-font candidate buried mid-page with no email anchor must lose to
    # the None fallback rather than guess:
    lines = [_line(f"Section {index} Overview 2024", 20.0, index) for index in range(10)]
    lines.append(_line("Alex Martin", 10.0, 10))
    assert pick_name(lines) is None


def test_tie_breaks_to_the_earlier_line():
    lines = [
        _line("Alex Martin", 12.0, 0),
        _line("Jamie Fox", 12.0, 1),
    ]
    assert pick_name(lines) == "Alex Martin"


# ============================================================================
# EMAIL EXTRACTION (pure scorer)
# ============================================================================

def test_picks_the_clean_header_email():
    lines = [
        _line("Jane Doe", 24.0, 0),
        _line("jane.doe@example.com | 07123 456789", 10.0, 1),
        _line("Education", 14.0, 2),
    ]
    assert pick_email(lines) == "jane.doe@example.com"


def test_email_split_across_two_lines_is_reassembled():
    lines = [
        _line("Jane Doe", 24.0, 0),
        _line("jane.doe@", 10.0, 1),
        _line("example.com", 10.0, 2),
    ]
    assert pick_email(lines) == "jane.doe@example.com"


def test_email_split_before_the_domain_dot_is_reassembled():
    lines = [
        _line("jane.doe@example", 10.0, 0),
        _line(".com", 10.0, 1),
    ]
    assert pick_email(lines) == "jane.doe@example.com"


def test_no_email_present_returns_none():
    lines = [
        _line("Jane Doe", 24.0, 0),
        _line("Quantitative Research Intern", 11.0, 1),
        _line("Imperial College London", 11.0, 2),
    ]
    assert pick_email(lines) is None


def test_picks_the_earliest_email_over_a_referees():
    # The candidate's own contact email heads the page; a referee's address
    # under References comes later and must lose to it, not overwrite it:
    lines = [
        _line("Jane Doe", 24.0, 0),
        _line("jane.doe@gmail.com", 10.0, 1),
        _line("References", 14.0, 2),
        _line("Prof. Alex Turner - alex.turner@imperial.ac.uk", 10.0, 3),
    ]
    assert pick_email(lines) == "jane.doe@gmail.com"


def test_role_mailbox_is_skipped_for_a_later_personal_address():
    # A generic shared mailbox earlier on the page is never the applicant -
    # skip it and keep scanning rather than returning it because it was first:
    lines = [
        _line("For general enquiries: careers@bigbank.com", 10.0, 0),
        _line("Jane Doe", 24.0, 1),
        _line("jane.doe@gmail.com", 10.0, 2),
    ]
    assert pick_email(lines) == "jane.doe@gmail.com"


def test_mailto_prefix_and_trailing_punctuation_are_stripped():
    lines = [_line("Contact: mailto:jane.doe@example.com.", 10.0, 0)]
    assert pick_email(lines) == "jane.doe@example.com"


def test_ligature_glyphs_do_not_break_matching():
    # PDF text extraction sometimes yields a ligature glyph ("fi") rather than
    # the two separate letters:
    lines = [_line("jane.ﬁnance@example.com", 10.0, 0)]
    assert pick_email(lines) == "jane.finance@example.com"


def test_only_role_mailboxes_present_returns_none():
    lines = [_line("careers@bigbank.com | hr@bigbank.com", 10.0, 0)]
    assert pick_email(lines) is None
