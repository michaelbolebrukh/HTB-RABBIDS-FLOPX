"""Header values that survive real user data.

A filename is user data: it can carry an apostrophe, a quote, a newline, or a
script that has no Latin-1 spelling at all. An HTTP header field, meanwhile, is
a Latin-1 byte string with structural meaning for quotes and CRLF - so pasting
one straight into the other is a bug with three separate faces:

- Non-Latin-1 characters (CJK, Cyrillic, Arabic, an emoji) make the ASGI server
  refuse to encode the response at all. The request 500s, permanently, for that
  one file - the user cannot fix it by retrying.
- A CR or LF would be header injection if the server allowed it; every serious
  one refuses instead, which turns it into the same permanent 500.
- A double quote silently ends the quoted string early and truncates the name.

`content_disposition` is the one place that has to think about this, so no call
site has to. Do not hand-build the header string anywhere else.
"""
from urllib.parse import quote


# Everything below 0x20 plus DEL: CR and LF are the injection vectors, the rest
# are simply not meaningful in a header and not meaningful in a filename:
_CONTROL_CHARACTERS = {chr(code) for code in range(0x20)} | {chr(0x7F)}

# The name shown when a filename reduces to nothing an ASCII client can use -
# for instance a name written entirely in a non-Latin script:
_FALLBACK_NAME = "download"


def content_disposition(disposition: str, filename: str) -> str:
    """A complete Content-Disposition value carrying `filename` safely.

    Emits both forms defined by RFC 6266: a sanitised ASCII `filename=` that
    every client understands, and a percent-encoded UTF-8 `filename*=` that
    modern clients prefer and that preserves the name exactly. Clients that
    understand both are required to prefer the second, so a name in any script
    arrives intact while an ancient client still gets something legible.

    The returned value is always pure ASCII, so it can never fail to encode.
    """
    cleaned = "".join(character for character in filename if character not in _CONTROL_CHARACTERS)

    # The quoted-string form: quotes and backslashes would end or escape the
    # string, and anything outside ASCII cannot be spelled here at all:
    ascii_name = cleaned.replace("\\", "").replace('"', "").encode("ascii", "ignore").decode("ascii").strip()
    ascii_name = ascii_name or _FALLBACK_NAME

    # The extended form: percent-encoded UTF-8, so the full name survives. RFC
    # 5987 reserves the attribute characters, hence the empty `safe` set:
    encoded_name = quote(cleaned or _FALLBACK_NAME, safe="", encoding="utf-8")

    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
