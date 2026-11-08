"""The CV-first parse preview's identity pipeline: strong deterministic
parser first, then an AI fallback ONLY for whichever field the parser missed.

THE BRANCH MATRIX
==================

The variables that drive `/talent/parse`'s observable behaviour:

    auth  : logged in | logged out
    email : found by parser | found by AI | not found at all
    name  : found by parser | found by AI | not found at all

Reachability
------------

`auth` does NOT gate the backend endpoint itself — `/talent/parse` carries no
`user_dependency` and behaves identically regardless of the caller's session,
by design (see product/routers/talent.py). It gates the CLIENT: a signed-in
member already has both an email and a name from their account, so the
CV-first parse-and-prefill dance exists only for a signed-out visitor.
`pickCv` in next-pwa/product/features/talent/components/talent-apply-card.tsx
returns before calling `parseTalentCv` whenever a user is signed in, so in
product use the endpoint is only ever reached with auth=logged-out.

That collapses the matrix to ONE reachable state for auth=logged-in ("the
endpoint is never called" — a frontend contract, not a pytest-testable server
branch) and the full 3x3 = 9 (email-source x name-source) combinations below
for auth=logged-out, all covered here:

    #  email source      name source       AI asked for           covered by
    -  ----------------  ----------------  ----------------------  ----------
    1  parser             parser            (none — no AI call)    test_both_found_by_parser_skips_ai
    2  parser             AI                name only               test_email_by_parser_name_by_ai
    3  parser             not found          name only (AI misses)  test_email_by_parser_name_not_found
    4  AI                 parser            email only               test_email_by_ai_name_by_parser
    5  AI                 AI                both                     test_both_found_by_ai
    6  AI                 not found          both (name misses)      test_email_by_ai_name_not_found
    7  not found          parser            email only (AI misses)   test_email_not_found_name_by_parser
    8  not found          AI                both (email misses)      test_email_not_found_name_by_ai
    9  not found          not found          both (neither found)    test_neither_found

Plus robustness cases outside the pure combinatorial matrix (same observable
"not found" cell, different REASON — worth covering separately since they
exercise different code paths):

    - No OPENAI_API_KEY configured (the feature dormant) -> same as parser-
      only, AI never even attempted (test_no_api_key_skips_ai_entirely).
    - The AI call raises / times out -> fails open, deterministic result
      stands (test_ai_error_fails_open).
    - Scanned CV (no usable text layer) -> the vision fallback engages
      instead of the text path (test_no_usable_text_uses_vision_fallback).
    - Plenty of page text -> the text path is used, bounded to
      _AI_TEXT_CHARS (test_usable_text_uses_text_fallback_bounded).
    - The parser's result for a field is NEVER overwritten by whatever the
      AI returns for that field, even if the AI (hypothetically) answers a
      field it was not asked about (test_parser_field_always_wins_over_ai).

Every test below builds a REAL minimal PDF and runs the REAL deterministic
parser (`extract_identity_from_pdf` calls `_read_first_page`/`pick_email`/
`pick_name` for real) — only the AI seam (`_ai_identity_fallback`, or one
level down `OpenAI` itself) is faked, so these tests never hit the network
(OPENAI_API_KEY is forced empty for the whole suite in the root conftest).


Every call here passes `allow_ai_fallback=True`, because the fallback IS the
subject. It defaults to FALSE in production and only signed-in callers opt in,
so an anonymous visitor's document never reaches a third-party model; that
branch is asserted at the router, in test_talent_router.py.
"""

from typing import Optional

from platform.services import document_identity


# ============================================================================
# MINIMAL PDF BUILDER (uniform font size — mirrors the router test's helper
# so name-extraction relies on the SAME font+position signals it does there)
# ============================================================================

def _lines_pdf(lines: list[str]) -> bytes:
    content_parts = []
    y = 720.0
    for text in lines:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_parts.append(f"BT /F1 11 Tf 72 {y:g} Td ({escaped}) Tj ET")
        y -= 20
    stream = "\n".join(content_parts).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# Deterministic parser outcome per fixture, confirmed directly against
# pick_name/pick_email before being relied on here:
PDF_BOTH_FOUND = _lines_pdf(["Jane Doe", "jane.doe@example.com"])
PDF_EMAIL_ONLY = _lines_pdf(["Curriculum Vitae", "Education", "jane.doe@example.com"])
PDF_NAME_ONLY = _lines_pdf(["Jane Doe", "Quantitative Research Intern"])
PDF_NEITHER_FOUND = _lines_pdf(["Objective", "Seeking a graduate role in finance"])


# ============================================================================
# FAKE AI SEAM (records every call so tests can assert whether/how the AI
# was invoked, without any network access)
# ============================================================================

def _fake_ai(name_result: Optional[str], email_result: Optional[str], calls: list[dict]):
    """Mirrors the real `_ai_identity_fallback` contract: only ever returns a
    non-None value for a field it was actually asked for."""

    def fake(data: bytes, page_text: str, need_name: bool, need_email: bool):
        calls.append({"need_name": need_name, "need_email": need_email})
        return (name_result if need_name else None), (email_result if need_email else None)

    return fake


# ============================================================================
# THE 9 REACHABLE (email-source x name-source) COMBINATIONS
# ============================================================================

def test_both_found_by_parser_skips_ai(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("AI Name", "ai@example.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_BOTH_FOUND, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": "jane.doe@example.com"}
    assert calls == []  # AI never called at all: both fields already resolved


def test_email_by_parser_name_by_ai(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("Jane Doe", "wrong@example.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_EMAIL_ONLY, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": "jane.doe@example.com"}
    assert calls == [{"need_name": True, "need_email": False}]


def test_email_by_parser_name_not_found(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai(None, "wrong@example.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_EMAIL_ONLY, allow_ai_fallback=True)

    assert result == {"name": None, "email": "jane.doe@example.com"}
    assert calls == [{"need_name": True, "need_email": False}]


def test_email_by_ai_name_by_parser(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("Wrong Name", "jane.doe@gmail.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_NAME_ONLY, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": "jane.doe@gmail.com"}
    assert calls == [{"need_name": False, "need_email": True}]


def test_email_not_found_name_by_parser(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("Wrong Name", None, calls))

    result = document_identity.extract_identity_from_pdf(PDF_NAME_ONLY, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": None}
    assert calls == [{"need_name": False, "need_email": True}]


def test_both_found_by_ai(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("Jane Doe", "jane.doe@example.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_NEITHER_FOUND, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": "jane.doe@example.com"}
    assert calls == [{"need_name": True, "need_email": True}]


def test_email_by_ai_name_not_found(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai(None, "jane.doe@example.com", calls))

    result = document_identity.extract_identity_from_pdf(PDF_NEITHER_FOUND, allow_ai_fallback=True)

    assert result == {"name": None, "email": "jane.doe@example.com"}
    assert calls == [{"need_name": True, "need_email": True}]


def test_email_not_found_name_by_ai(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai("Jane Doe", None, calls))

    result = document_identity.extract_identity_from_pdf(PDF_NEITHER_FOUND, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": None}
    assert calls == [{"need_name": True, "need_email": True}]


def test_neither_found(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(document_identity, "_ai_identity_fallback", _fake_ai(None, None, calls))

    result = document_identity.extract_identity_from_pdf(PDF_NEITHER_FOUND, allow_ai_fallback=True)

    assert result == {"name": None, "email": None}
    assert calls == [{"need_name": True, "need_email": True}]


# ============================================================================
# THE PARSER ALWAYS WINS (per field, even against a misbehaving AI)
# ============================================================================

def test_parser_field_always_wins_over_ai(monkeypatch):
    # A fake that violates the real contract on purpose — answering a field
    # it was NOT asked for — to prove the merge itself (`name or ai_name`)
    # is what keeps the parser's field authoritative, not good AI behaviour:
    def misbehaving(data, page_text, need_name, need_email):
        return "AI Overwrote The Name", "ai-overwrote@example.com"

    monkeypatch.setattr(document_identity, "_ai_identity_fallback", misbehaving)

    result = document_identity.extract_identity_from_pdf(PDF_BOTH_FOUND, allow_ai_fallback=True)

    assert result == {"name": "Jane Doe", "email": "jane.doe@example.com"}


def test_parser_name_survives_an_ai_asked_only_for_the_email(monkeypatch):
    """The merge itself must be what protects a parser-found field.

    The test above cannot prove that: with BOTH fields parsed, the fallback is
    never invoked at all, so `name or ai_name` is never exercised and flipping
    it to `ai_name or name` passes unnoticed. This case forces the fallback to
    run (the email is genuinely missing) while the AI also volunteers a name it
    was not asked for, so only a correct merge keeps the parser's name.
    """
    def misbehaving(data, page_text, need_name, need_email):
        # Asked for the email only, but answering both, exactly as a
        # misbehaving or prompt-injected model would:
        assert need_email and not need_name
        return "AI Overwrote The Name", "ai@example.com"

    monkeypatch.setattr(document_identity, "_ai_identity_fallback", misbehaving)

    result = document_identity.extract_identity_from_pdf(PDF_NAME_ONLY, allow_ai_fallback=True)

    assert result["name"] == "Jane Doe", "the parser's name must outrank the AI's"
    assert result["email"] == "ai@example.com"


# ============================================================================
# ROBUSTNESS: OUTSIDE THE PURE COMBINATORIAL MATRIX
# ============================================================================

def test_no_api_key_skips_ai_entirely(monkeypatch):
    # The root conftest already clears OPENAI_API_KEY for the whole suite;
    # this asserts the CONSEQUENCE explicitly: the OpenAI client is never
    # even constructed when the key is unset:
    monkeypatch.setattr(document_identity.s, "OPENAI_API_KEY", None)
    constructed = []
    # The client is built once per process and cached, so a test that swaps
    # `OpenAI` must also clear the cache or it inherits the previous test's
    # client (monkeypatch restores both afterwards):
    monkeypatch.setattr(document_identity, "_ai_client_cache", None)
    monkeypatch.setattr(document_identity, "OpenAI", lambda **kw: constructed.append(kw) or None)

    result = document_identity.extract_identity_from_pdf(PDF_NEITHER_FOUND, allow_ai_fallback=True)

    assert result == {"name": None, "email": None}
    assert constructed == []


def test_ai_error_fails_open(monkeypatch):
    monkeypatch.setattr(document_identity.s, "OPENAI_API_KEY", "sk-test-dummy")

    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                def parse(**kwargs):
                    raise RuntimeError("simulated network failure")

    # The client is built once per process and cached, so a test that swaps
    # `OpenAI` must also clear the cache or it inherits the previous test's
    # client (monkeypatch restores both afterwards):
    monkeypatch.setattr(document_identity, "_ai_client_cache", None)
    monkeypatch.setattr(document_identity, "OpenAI", lambda **kw: _BoomClient())

    # The parser finds the email but not the name; the AI call errors trying
    # to fill the name gap, and the request must still succeed with the
    # deterministic result intact — never a broken upload:
    result = document_identity.extract_identity_from_pdf(PDF_EMAIL_ONLY, allow_ai_fallback=True)

    assert result == {"name": None, "email": "jane.doe@example.com"}


# ============================================================================
# TEXT VS VISION SELECTION (inside `_ai_identity_fallback` itself, one level
# below the wrapper faked above)
# ============================================================================

class _FakeParsedMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed):
        self.message = _FakeParsedMessage(parsed)


class _FakeParseResponse:
    def __init__(self, parsed):
        self.choices = [_FakeChoice(parsed)]


def _fake_openai_client(monkeypatch, parsed=None, capture: Optional[list] = None):
    """Monkeypatches `document_identity.OpenAI` with a fake whose
    `.chat.completions.parse(**kwargs)` records its kwargs and returns a
    canned parsed result — no network, no real SDK object."""
    captured = capture if capture is not None else []

    class _FakeCompletions:
        @staticmethod
        def parse(**kwargs):
            captured.append(kwargs)
            return _FakeParseResponse(parsed)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    # The client is built once per process and cached, so a test that swaps
    # `OpenAI` must also clear the cache or it inherits the previous test's
    # client (monkeypatch restores both afterwards):
    monkeypatch.setattr(document_identity, "_ai_client_cache", None)
    monkeypatch.setattr(document_identity, "OpenAI", lambda **kw: _FakeClient())
    monkeypatch.setattr(document_identity.s, "OPENAI_API_KEY", "sk-test-dummy")
    return captured


def test_usable_text_uses_text_fallback_bounded(monkeypatch):
    parsed = document_identity._CvIdentityFields(name="Jane Doe", email=None)
    captured = _fake_openai_client(monkeypatch, parsed=parsed)
    long_text = "Jane Doe\n" + ("Filler line describing experience. " * 500)

    name, email = document_identity._ai_identity_fallback(
        PDF_BOTH_FOUND, long_text, need_name=True, need_email=False,
    )

    assert (name, email) == ("Jane Doe", None)
    assert len(captured) == 1
    content = captured[0]["messages"][0]["content"]
    # Text-only content — no image part when the page has usable text:
    assert isinstance(content, list) and len(content) == 1
    assert content[0]["type"] == "text"
    assert len(content[0]["text"]) <= document_identity._AI_TEXT_CHARS + 500  # instruction + bounded slice


def test_no_usable_text_uses_vision_fallback(monkeypatch):
    parsed = document_identity._CvIdentityFields(name="Jane Doe", email="jane.doe@example.com")
    captured = _fake_openai_client(monkeypatch, parsed=parsed)
    monkeypatch.setattr(
        document_identity, "_render_first_page_to_data_url", lambda data: "data:image/png;base64,AAAA"
    )

    name, email = document_identity._ai_identity_fallback(
        PDF_BOTH_FOUND, "", need_name=True, need_email=True,
    )

    assert (name, email) == ("Jane Doe", "jane.doe@example.com")
    content = captured[0]["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)


def test_vision_fallback_gives_up_when_rendering_fails(monkeypatch):
    monkeypatch.setattr(document_identity.s, "OPENAI_API_KEY", "sk-test-dummy")
    constructed = []
    # The client is built once per process and cached, so a test that swaps
    # `OpenAI` must also clear the cache or it inherits the previous test's
    # client (monkeypatch restores both afterwards):
    monkeypatch.setattr(document_identity, "_ai_client_cache", None)
    monkeypatch.setattr(document_identity, "OpenAI", lambda **kw: constructed.append(kw) or None)
    monkeypatch.setattr(document_identity, "_render_first_page_to_data_url", lambda data: None)

    name, email = document_identity._ai_identity_fallback(
        b"not a real pdf", "", need_name=True, need_email=True,
    )

    assert (name, email) == (None, None)
    assert constructed == []  # Never even reaches the OpenAI call


def test_ai_never_called_when_nothing_is_missing():
    # need_name=False, need_email=False can happen if a caller asks with
    # nothing outstanding; must short-circuit before touching settings/the
    # client at all:
    assert document_identity._ai_identity_fallback(
        PDF_BOTH_FOUND, "Jane Doe", need_name=False, need_email=False,
    ) == (None, None)


# ============================================================================
# THE COST CEILINGS (the parts of this module that exist for safety rather
# than accuracy, and that every other test in this file monkeypatches away)
# ============================================================================

def _oversized_page_pdf(side_points: int) -> bytes:
    """A one-page PDF that simply DECLARES an enormous page. Legal, tiny, and
    the whole attack: at a fixed render scale the bitmap is the square of this
    number, so a few hundred bytes buy gigabytes of someone else's RAM."""
    stream = b"BT /F1 11 Tf 72 72 Td (x) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
        + f"{side_points} {side_points}".encode()
        + b"] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def _rendered_size(data_url: str) -> tuple[int, int]:
    """Width and height straight out of the PNG's IHDR chunk, so the assertion
    is about the bitmap that was actually produced, not about the code path
    that produced it."""
    import base64
    import struct

    png = base64.b64decode(data_url.split(",", 1)[1])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", png[16:24])


def test_an_enormous_declared_page_still_renders_within_the_pixel_ceiling():
    """The render clamp, exercised for real rather than monkeypatched.

    Every other test here replaces `_render_first_page_to_data_url` with a
    stub, so deleting the clamp inside it changed nothing anywhere in the
    suite — while in production a 585-byte upload became ~5.7GB of bitmap on
    an anonymous request. This renders the real thing and measures the result.
    """
    data_url = document_identity._render_first_page_to_data_url(_oversized_page_pdf(14400))

    assert data_url is not None
    width, height = _rendered_size(data_url)
    assert max(width, height) <= document_identity._RENDER_MAX_PX


def test_an_ordinary_page_is_not_shrunk_by_the_ceiling():
    """The clamp must be a ceiling, not a resize: an A4-ish page still renders
    at the intended scale, or the vision fallback quietly loses the resolution
    it needs to read a header."""
    data_url = document_identity._render_first_page_to_data_url(_oversized_page_pdf(612))

    assert data_url is not None
    width, height = _rendered_size(data_url)
    assert width == round(612 * document_identity._RENDER_SCALE)


def test_a_pathological_page_is_refused_by_the_parser_and_answered_anyway():
    """A content stream with a million text operators: legal, ~60KB, and twenty
    seconds of layout work per two megabytes. `/talent/parse` is anonymous, so
    the only thing between this file and a stalled worker is the content
    ceiling. Measured, because a ceiling nobody times is a comment.

    The result is empty rather than an error: the file told us nothing, the
    visitor is asked to type their details, and the request is over.
    """
    import time

    bomb = _content_bomb_pdf(1_000_000)
    assert len(bomb) < 200_000  # Nothing about the upload looks wrong

    start = time.monotonic()
    lines, author = document_identity._read_first_page(bomb)
    elapsed = time.monotonic() - start

    assert (lines, author) == ([], None)
    # Unbounded this runs for minutes; the refusal is one bounded decompression:
    assert elapsed < 1.0, f"the refusal itself took {elapsed:.1f}s"


def test_an_ordinary_cv_is_nowhere_near_the_content_ceiling():
    """The ceiling must be invisible to real documents, or it is a bug that
    only shows up on somebody's actual CV."""
    assert document_identity._page_one_content_is_boundable(PDF_BOTH_FOUND) is True
    assert document_identity._read_first_page(PDF_BOTH_FOUND)[0] != []


def test_an_uncompressed_page_is_measured_by_its_own_length():
    """No filter at all is the easy case, and it must still be checked - an
    attacker can simply not compress, and the 10MB upload cap alone still
    leaves room for hundreds of thousands of operators."""
    over = _content_bomb_pdf(200_000, compress=False)
    assert document_identity._page_one_content_is_boundable(over) is False


def _content_bomb_pdf(operations: int, compress: bool = True) -> bytes:
    import zlib

    stream = b"BT /F1 8 Tf\n" + b"1 0 0 1 10 700 Tm (A) Tj\n" * operations + b"ET\n"
    if compress:
        body = zlib.compress(stream, 9)
        entry = b"<< /Length " + str(len(body)).encode() + b" /Filter /FlateDecode >>\nstream\n"
    else:
        body = stream
        entry = b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        entry + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, item in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + item + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def test_a_pathological_page_is_not_handed_to_the_renderer_either():
    """The ceiling has to cover BOTH paths or it only moves the cost.

    A refused parse produces no text, and no text is exactly what routes a
    file to the vision fallback — so a ceiling on the parser alone hands every
    bomb to the more expensive of the two. Measured before this guard existed:
    2.5 seconds and 386MB to render one million operators into an ordinary
    A4-sized bitmap, which the pixel clamp cannot see and cannot prevent.
    """
    import time

    bomb = _content_bomb_pdf(1_000_000)

    start = time.monotonic()
    assert document_identity._render_first_page_to_data_url(bomb) is None
    assert time.monotonic() - start < 1.0


def test_the_renderer_still_serves_an_ordinary_scanned_page():
    """The guard must not close the vision path it exists to protect: a page
    with no text layer is the whole reason that path exists."""
    scanned = _lines_pdf([])
    assert document_identity._render_first_page_to_data_url(scanned) is not None
