"""Inbound HTML safety profile and inline-image resolution."""

import base64

import pytest

from platform.services.inbox_service.ports import AttachmentMeta
from platform.services.inbox_service.sanitize import (
    infer_outbound_color_scheme,
    prepare_outbound_html,
    resolve_cid_images,
    sanitize_email_html,
    wrap_outbound_mail_html,
)
from platform.services.inbox_service.service import _text_fallback


def test_scripts_handlers_and_forms_are_stripped():
    dirty = (
        '<p onclick="steal()" style="color:red">hi</p>'
        "<script>alert(1)</script>"
        '<form action="https://evil"><input name="pw"></form>'
        '<iframe src="https://evil"></iframe>'
    )
    clean = sanitize_email_html(dirty)
    assert "<script" not in clean and "onclick" not in clean
    assert "<form" not in clean and "<input" not in clean and "<iframe" not in clean
    # Inline styles survive - email layout depends on them:
    assert 'style="color:red"' in clean


def test_dangerous_url_schemes_are_dropped_but_cid_and_data_survive():
    dirty = (
        '<a href="javascript:alert(1)">x</a>'
        '<img src="cid:inline1@probe">'
        '<img src="data:image/png;base64,AAAA">'
        '<img src="https://remote/pixel.png">'
    )
    clean = sanitize_email_html(dirty)
    assert "javascript:" not in clean
    assert 'src="cid:inline1@probe"' in clean
    assert 'src="data:image/png;base64,AAAA"' in clean
    # Remote images stay in the markup - the frontend iframe CSP gates loading:
    assert 'src="https://remote/pixel.png"' in clean


def test_links_get_hardened_rel():
    clean = sanitize_email_html('<a href="https://ok.example">x</a>')
    assert 'rel="noopener noreferrer"' in clean


def test_outbound_signature_table_chrome_survives():
    """Presentational table attrs must survive outbound sanitize (signature card)."""
    dirty = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" style="border-collapse:collapse">'
        "<tr>"
        '<td bgcolor="#0b64f4" width="4" height="48" align="left" valign="top"></td>'
        '<td bgcolor="#111827" style="padding:12px;color:#fff">Hi</td>'
        "</tr>"
        "</table>"
    )
    clean = sanitize_email_html(dirty)
    assert 'bgcolor="#0b64f4"' in clean
    assert 'bgcolor="#111827"' in clean
    assert 'role="presentation"' in clean
    assert 'cellpadding="0"' in clean
    assert 'cellspacing="0"' in clean
    assert 'width="4"' in clean
    assert 'valign="top"' in clean
    assert "padding:12px" in clean


def test_signature_surface_background_attr_survives_outbound_sanitize():
    """Solid PNG card fills use background= so Gmail DM does not wash hex chrome."""
    dirty = (
        '<table role="presentation"><tr>'
        '<td bgcolor="#12151c" background="https://www.example.com/email-sig-surface-dark.png" '
        'style="background-color:#12151c;'
        "background-image:url('https://www.example.com/email-sig-surface-dark.png')\">x</td>"
        "</tr></table>"
    )
    clean = sanitize_email_html(dirty)
    assert 'background="https://www.example.com/email-sig-surface-dark.png"' in clean
    assert "background-image:url(" in clean
    assert 'bgcolor="#12151c"' in clean


def test_outbound_color_scheme_wrap_is_always_light_only():
    """Dark visual cards still declare light only — appearance is PNG + inline."""
    dark_body = (
        '<td bgcolor="#12151c">'
        '<div class="qs-gmail-blend-screen"><div class="qs-gmail-blend-difference">'
        "dark card</div></div></td>"
    )
    light_body = '<td bgcolor="#ffffff">light card</td>'
    assert infer_outbound_color_scheme(dark_body) == "light"
    assert infer_outbound_color_scheme(light_body) == "light"
    wrapped_dark = wrap_outbound_mail_html(dark_body)
    wrapped_light = wrap_outbound_mail_html(light_body)
    assert 'name="color-scheme" content="light only"' in wrapped_dark
    assert "color-scheme:light only" in wrapped_dark
    assert 'class="body"' in wrapped_dark
    assert "qs-gmail-blend-screen" in wrapped_dark
    assert "mix-blend-mode:screen" in wrapped_dark
    assert 'name="color-scheme" content="light only"' in wrapped_light
    assert "color-scheme:light only" in wrapped_light
    assert "mix-blend-mode:screen" not in wrapped_light
    # Explicit dark kwarg is coerced — do not reintroduce the failing strategy:
    assert 'content="dark"' not in wrap_outbound_mail_html(dark_body, color_scheme="dark")
    # prepare_outbound_html must wrap *after* sanitize (meta cannot be left to nh3):
    prepared = prepare_outbound_html(
        '<table><tr><td bgcolor="#12151c" onclick="x">'
        '<div class="qs-gmail-blend-screen">hi</div>'
        "<script>bad()</script></td></tr></table>"
    )
    assert 'name="color-scheme" content="light only"' in prepared
    assert 'bgcolor="#12151c"' in prepared
    assert 'class="qs-gmail-blend-screen"' in prepared
    assert "<script" not in prepared
    assert "onclick" not in prepared


def test_tel_and_mailto_links_survive_outbound_sanitize():
    """Signature phone rows use tel:; mailto must keep working too."""
    dirty = (
        '<a href="tel:+15551212">+1 555 1212</a>'
        '<a href="mailto:hello@example.com">hello@example.com</a>'
        '<a href="javascript:alert(1)">x</a>'
    )
    clean = sanitize_email_html(dirty)
    assert 'href="tel:+15551212"' in clean
    assert 'href="mailto:hello@example.com"' in clean
    assert "javascript:" not in clean


def test_cid_images_resolve_to_data_uris():
    html = '<p><img src="cid:dot@p"><img src="cid:missing@p"></p>'
    attachments = [AttachmentMeta(index=0, filename="dot.png", content_type="image/png",
                                  size=3, cid="dot@p", is_inline=True)]
    resolved = resolve_cid_images(html, attachments, get_bytes=lambda i: b"\x01\x02\x03")

    encoded = base64.b64encode(b"\x01\x02\x03").decode()
    assert f'src="data:image/png;base64,{encoded}"' in resolved
    # Unknown cid is left alone rather than broken:
    assert 'src="cid:missing@p"' in resolved


def test_text_fallback_flattens_composed_html():
    text = _text_fallback("<p>Hello <b>there</b></p><p>Second&nbsp;line</p>")
    assert text == "Hello there\nSecond line"


def test_the_composer_signature_marker_class_survives_sanitizing():
    """A cross-language contract, so it is pinned on the side that can break it.

    The composer stamps `qs-outbound-signature` on the signature block it
    appends, so a message re-opened for editing can have the block cut out
    rather than fed through the editor — whose schema has no table node, and so
    shreds a signature left in the body and then signs over the wreckage.

    Every composed message passes through here on its way out, and a saved
    draft is stored sanitized, so a narrowed `class` allowlist would strip the
    marker from those copies with nothing on the frontend able to notice. (The
    scheduled outbox is the one path that does NOT sanitize — it freezes the
    composer's html verbatim — so that restore would survive this changing.
    The guard is for the paths that do.)

    See next-pwa/platform/inbox/scheduled-restore.ts.
    """
    import re
    from pathlib import Path

    # Read the class out of the TypeScript that defines it, rather than copying
    # the literal here where a frontend rename would leave it silently passing.
    # Source-reading is the sanctioned shape for a cross-language drift guard
    # (test-integrity), and it is the only way this side can see that rename.
    source = (
        Path(__file__).resolve().parents[3]
        / "next-pwa"
        / "platform"
        / "inbox"
        / "scheduled-restore.ts"
    )
    if not source.exists():
        pytest.skip("frontend package not present in this checkout")
    match = re.search(
        r'OUTBOUND_SIGNATURE_MARKER_CLASS\s*=\s*"([^"]+)"', source.read_text()
    )
    assert match, f"marker constant not found in {source}"
    marker = match.group(1)

    marked = (
        "<p>Hello</p><p></p>"
        f'<div class="{marker}">'
        '<table><tr><td><div class="qs-gmail-blend-dark">Ada</div></td></tr></table>'
        "</div>"
    )
    assert f'class="{marker}"' in sanitize_email_html(marked)
    assert f'class="{marker}"' in prepare_outbound_html(marked)
