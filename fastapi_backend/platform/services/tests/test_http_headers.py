"""Content-Disposition has to survive whatever a person names a file.

Each test below corresponds to a filename that DID break, or would have
broken, a real download: a name in a non-Latin script (a permanent 500, since
the header cannot be encoded), a name carrying CRLF (rejected by the HTTP
layer, so also a permanent 500, and header injection if it were not), and a
name carrying a quote (silent truncation).
"""
import pytest

from platform.utils.http_headers import content_disposition


def _encodes_as_a_header(value: str) -> bytes:
    """What the ASGI server does with it. Latin-1 is the whole contract for a
    header value; failing this is the 500 these tests exist to prevent."""
    return value.encode("latin-1")


@pytest.mark.parametrize(
    "filename",
    [
        "张伟 - CV.pdf",
        "김민준 - CV.pdf",
        "Ольга Иванова - CV.pdf",
        "سيرة ذاتية.pdf",
        "Zoë Müller — CV.pdf",
        "résumé 📄.pdf",
    ],
)
def test_a_name_in_any_script_still_produces_a_sendable_header(filename):
    value = content_disposition("inline", filename)
    _encodes_as_a_header(value)
    # The exact name survives in the extended form even when the ASCII
    # fallback cannot spell a single character of it:
    assert "filename*=UTF-8''" in value


@pytest.mark.parametrize("injected", ["\r\n", "\n", "\r", "\x00"])
def test_control_characters_never_reach_the_header(injected):
    value = content_disposition("attachment", f"Evil{injected}X-Injected: yes.pdf")
    assert injected not in value
    _encodes_as_a_header(value)


def test_a_quote_cannot_end_the_quoted_string_early():
    value = content_disposition("attachment", 'Jane "J" Doe - CV.pdf')
    # One opening and one closing quote around the ASCII form, no more:
    assert value.count('"') == 2
    assert "Jane J Doe - CV.pdf" in value


def test_a_backslash_cannot_escape_the_closing_quote():
    value = content_disposition("attachment", 'trap\\".pdf')
    assert value.count('"') == 2
    _encodes_as_a_header(value)


def test_a_name_with_no_ascii_at_all_still_names_something():
    value = content_disposition("attachment", "张伟")
    # A client that only understands the plain form gets a usable name rather
    # than an empty one, which some clients treat as a missing header:
    assert 'filename=""' not in value


def test_the_ordinary_case_reads_normally():
    assert content_disposition("inline", "Jane Doe - CV.pdf") == (
        'inline; filename="Jane Doe - CV.pdf"; filename*=UTF-8\'\'Jane%20Doe%20-%20CV.pdf'
    )
