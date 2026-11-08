"""HTML safety for the inbox.

Two responsibilities, both delegated to maintained tools rather than owned:

- Inbound mail is hostile HTML: nh3 (ammonia bindings) strips scripts, event
  handlers, forms, and dangerous URLs server-side, so the API never emits
  unsanitized markup. The frontend additionally renders the result inside a
  sandboxed iframe whose CSP governs remote loads (images stay blocked until
  the user reveals them) - two independent layers.
- Inline (cid) images are resolved into data: URIs here, because an iframe
  document cannot attach our auth header to fetch them separately.

Outbound HTML from the composer runs through the same profile before sending.
"""
import base64
import re

import nh3

from platform.services.inbox_service.ports import AttachmentMeta


# Emails depend on inline styles for layout, so `style` joins nh3's defaults.
# CSS cannot execute script; what it CAN do is reference remote urls, and the
# frontend iframe CSP is the layer that governs remote loading.
# Presentational table attrs (bgcolor, width, …) stay allowed so outbound
# signature chrome and typical mail HTML survive send without TipTap round-trip:
_TABLE_PRESENTATION = {
    "bgcolor",
    # Solid-fill PNG fallback so Gmail dark mode does not recolor card chrome
    # (bgcolor/CSS alone are rewritten; raster backgrounds usually are not):
    "background",
    "width",
    "height",
    "cellpadding",
    "cellspacing",
    "border",
    "align",
    "valign",
    "role",
}
_ALLOWED_ATTRIBUTES = {
    **nh3.ALLOWED_ATTRIBUTES,
    # `class` survives so Gmail-targeted blend wrappers (hteumeuleu) in dark
    # signature chrome can be selected from the outbound wrap <style>:
    "*": (nh3.ALLOWED_ATTRIBUTES.get("*") or set()) | {"style", "class"},
    "img": (nh3.ALLOWED_ATTRIBUTES.get("img") or set()) | {"src", "alt", "width", "height"},
    "table": (nh3.ALLOWED_ATTRIBUTES.get("table") or set()) | _TABLE_PRESENTATION,
    "td": (nh3.ALLOWED_ATTRIBUTES.get("td") or set()) | _TABLE_PRESENTATION | {"colspan", "rowspan"},
    "th": (nh3.ALLOWED_ATTRIBUTES.get("th") or set()) | _TABLE_PRESENTATION | {"colspan", "rowspan"},
    "tr": (nh3.ALLOWED_ATTRIBUTES.get("tr") or set()) | {"align", "valign", "bgcolor"},
}

# cid: survives sanitization so inline images can be resolved afterwards;
# data: survives because that resolution produces data: URIs.
# tel: survives so outbound signature (and normal mail) phone links stay dialable:
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel", "cid", "data"}

_CID_SRC_RE = re.compile(r'src=(["\'])cid:([^"\']+)\1', re.IGNORECASE)


def sanitize_email_html(html: str) -> str:
    """Strips scripts/handlers/forms from untrusted email HTML"""
    return nh3.clean(
        html,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )


def infer_outbound_color_scheme(html: str) -> str:
    """Outbound mail always declares `light only`.

    Appearance for dark signature chrome comes from solid PNG fills + authored
    inline colours — not from telling clients the message is a dark document.
    Declaring `dark` correlated with Gmail mobile still contrast-repairing light
    text on those PNG islands; `light only` matches the working LIGHT path.
    """
    _ = html  # body markers no longer choose dark meta
    return "light"


def _outbound_has_dark_signature_chrome(html: str) -> bool:
    """True when the body carries dark signature card markers (blend CSS needed)."""
    sample = html or ""
    return (
        "#12151c" in sample
        or "email-sig-surface-dark" in sample
        or "email-signature-dark" in sample
        or "qs-gmail-blend-screen" in sample
    )


def wrap_outbound_mail_html(html: str, *, color_scheme: str | None = None) -> str:
    """Wrap sanitized body HTML in a document that discourages Gmail dark-mode
    recolor of authored signature text/chrome.

    Must run *after* sanitize_email_html — nh3 strips html/head/meta. Always
    emit `color-scheme: light only` so clients that listen do not invent a
    second theme against bulletproof (PNG) card fills. Dark visual cards still
    look dark via PNGs + inline colours; when dark chrome is present, also emit
    Gmail-targeted mix-blend-mode rules (hteumeuleu) for light-on-dark type.
    """
    body = html or ""
    # Avoid double-wrapping if a caller already supplied a document:
    if re.search(r"<html\b", body, flags=re.IGNORECASE):
        return body
    # color_scheme kwarg kept for call-site compat; dark is coerced to light only:
    scheme = color_scheme or infer_outbound_color_scheme(body)
    if scheme == "dark":
        scheme = "light"
    meta = "light only"
    css = "light only"
    blend_css = ""
    if _outbound_has_dark_signature_chrome(body):
        # Gmail replaces doctype with <u></u>; u + .body targets Gmail only so
        # non-Gmail clients never get the black blend layers:
        blend_css = (
            "u + .body .qs-gmail-blend-screen{background:#000;mix-blend-mode:screen;}"
            "u + .body .qs-gmail-blend-difference{background:#000;mix-blend-mode:difference;}"
        )
    return (
        "<!DOCTYPE html>"
        '<html lang="en" xmlns="http://www.w3.org/1999/xhtml">'
        "<head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />'
        f'<meta name="color-scheme" content="{meta}" />'
        f'<meta name="supported-color-schemes" content="{meta}" />'
        f"<style>:root{{color-scheme:{css};}}body{{margin:0;padding:0;}}{blend_css}</style>"
        "</head>"
        f'<body class="body" style="margin:0;padding:0;">{body}</body>'
        "</html>"
    )


def prepare_outbound_html(html: str) -> str:
    """Sanitize then wrap — single call site for compose/reply/forward/draft."""
    return wrap_outbound_mail_html(sanitize_email_html(html))


def resolve_cid_images(html: str, attachments: list[AttachmentMeta], get_bytes) -> str:
    """Replaces cid: image references with data: URIs from the inline attachments.

    `get_bytes(index) -> bytes` defers content fetching to the caller so this
    stays provider-neutral and testable.
    """
    by_cid = {att.cid: att for att in attachments if att.cid}

    def _replace(match: re.Match) -> str:
        quote, cid = match.group(1), match.group(2)
        att = by_cid.get(cid) or by_cid.get(f"<{cid}>")
        if att is None:
            return match.group(0)
        data = get_bytes(att.index)
        if not data:
            return match.group(0)
        encoded = base64.b64encode(data).decode()
        return f"src={quote}data:{att.content_type};base64,{encoded}{quote}"

    return _CID_SRC_RE.sub(_replace, html)
