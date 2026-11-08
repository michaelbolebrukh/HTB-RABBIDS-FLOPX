"""Rendering email templates: the subject header versus the autoescaped body.

The subject line is extracted from the RENDERED html, which Jinja autoescapes.
An email Subject header is plain text, so the extraction must undo that
escaping - otherwise a subject containing "&" ships as "&amp;". The body must
STAY escaped: it is html, and unescaping it would reintroduce injection.
"""

from platform.enums import EmailTemplateType
from platform.services.email_services import render_template
from pathlib import Path
import re


def test_the_subject_is_plain_text_not_html_escaped():
    subject, html_body, text_body = render_template(
        EmailTemplateType.ADMIN_NOTIFICATION,
        subject="Runway & anchor <moved>: 9 days \"left\"",
        heading="Heading & sub",
        body="Body & <script>alert(1)</script>",
        severity="warning",
        severity_critical=False,
        cta_url="",
        cta_label="",
    )

    # The header carries the literal characters the caller passed:
    assert subject == 'Runway & anchor <moved>: 9 days "left"'
    assert "&amp;" not in subject and "&lt;" not in subject

    # ...while the html body keeps its escaping - that is what makes the
    # variable interpolation safe in the first place:
    assert "Heading &amp; sub" in html_body
    assert "&lt;script&gt;" in html_body
    assert "<script>alert(1)</script>" not in html_body

    # The txt variant is not autoescaped and carries the raw text:
    assert "Body & <script>alert(1)</script>" in text_body


def test_a_subject_with_no_escapable_characters_is_untouched():
    subject, _, _ = render_template(
        EmailTemplateType.ADMIN_NOTIFICATION,
        subject="Plain subject line",
        heading="H",
        body="B",
        severity="",
        severity_critical=False,
        cta_url="",
        cta_label="",
    )

    assert subject == "Plain subject line"


def test_sendable_templates_match_the_admin_catalog_and_generated_pairs():
    repository = Path(__file__).resolve().parents[4]
    registry = (repository / "next-pwa/platform/emails/registry.tsx").read_text()
    catalog = re.findall(r'\bname: "([a-z_]+)",', registry)
    sendable = {template.value for template in EmailTemplateType}
    assert len(catalog) == len(set(catalog))
    assert set(catalog) == sendable
    templates = Path(__file__).resolve().parents[1] / "email_services/templates"
    for extension in ("html", "txt"):
        assert {file.stem for file in templates.glob(f"*.{extension}")} == sendable


def test_privacy_notice_renders_date_and_summary_without_html_injection():
    subject, html_body, text_body = render_template(
        EmailTemplateType.PRIVACY_POLICY_CHANGE,
        name="Example member",
        effective_date="9 November 2026",
        change_summary='Controller & retention details.\n\n<script>alert("injection")</script>',
        policy_url="https://example.com/privacy-policy",
    )
    # The Terms link is the instance's own canonical origin, which the frontend
    # build bakes into the template from the same identity the manifest mirrors.
    # Reading the domain from the manifest keeps this test true on every clone
    # instead of pinning the first instance's hostname.
    from infrastructure.instance_manifest import get_instance_manifest

    manifest = get_instance_manifest()
    assert manifest is not None and manifest.domain
    assert subject == "Privacy policy and Terms update"
    for body in (html_body, text_body):
        assert "9 November 2026" in body
        assert "https://example.com/privacy-policy" in body
        assert "{{" not in body
        assert "Example member" in body
        assert f"{manifest.domain}/terms-of-service" in body
        assert "You can close your account free of charge if you disagree." in body
    assert "Controller &amp; retention details." in html_body
    assert "&lt;script&gt;" in html_body
    assert "<script>" not in html_body
    assert "Controller & retention details.\n\n<script>" in text_body


def test_every_sendable_template_uses_the_reserved_frontend_origin_for_local_brand_images(monkeypatch):
    from infrastructure.settings import settings
    from urllib.parse import urlsplit

    monkeypatch.setattr(settings, "PUBLIC_FRONTEND_ORIGIN", "https://preview.example.test")
    for template_type in EmailTemplateType:
        _, html_body, _ = render_template(
            template_type,
            email_asset_origin="https://attacker.example.test",
        )
        images = re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', html_body)
        assert len(images) == 2, template_type
        preloads = re.findall(
            r'<link\b(?=[^>]*\brel="preload")(?=[^>]*\bas="image")[^>]*\bhref="([^"]+)"',
            html_body,
        )
        assert preloads == images, template_type
        assert all(urlsplit(src).scheme == "https" for src in images), template_type
        assert all(urlsplit(src).netloc == "preview.example.test" for src in images), template_type
        assert all(urlsplit(src).path.startswith("/") for src in images), template_type
        assert "{{email_asset_origin}}" not in html_body, template_type
        assert "attacker.example.test" not in html_body, template_type


def test_email_asset_origin_never_rewrites_recipient_values_or_canonical_links(monkeypatch):
    from infrastructure.settings import settings

    variables = {
        "name": 'Keep https://www.quant-soc.com/logos/logo-light.png & <member>',
        "effective_date": "9 November 2026",
        "change_summary": "Retain https://www.quant-soc.com/logos/email-footer-wordmark.png in prose.",
        "policy_url": "https://www.example.test/privacy?member=27&token=unchanged",
    }
    monkeypatch.setattr(settings, "PUBLIC_FRONTEND_ORIGIN", "https://public.example.test")
    subject_before, html_before, text_before = render_template(EmailTemplateType.PRIVACY_POLICY_CHANGE, **variables)
    links_before = re.findall(r'<a\b[^>]*\bhref="([^"]+)"', html_before)
    monkeypatch.setattr(settings, "PUBLIC_FRONTEND_ORIGIN", "https://preview.example.test")
    subject, html_body, text_body = render_template(EmailTemplateType.PRIVACY_POLICY_CHANGE, **variables)
    assert subject == subject_before == "Privacy policy and Terms update"
    assert text_body == text_before
    assert variables["name"] in text_body
    assert 'Keep https://www.quant-soc.com/logos/logo-light.png &amp; &lt;member&gt;' in html_body
    assert variables["change_summary"] in html_body
    assert re.findall(r'<a\b[^>]*\bhref="([^"]+)"', html_body) == links_before
    assert 'href="https://www.example.test/privacy?member=27&amp;token=unchanged"' in html_body
    assert '<img src="https://preview.example.test/' in html_body


def test_email_images_use_the_existing_normalized_domain_fallback_when_no_frontend_origin_is_set(monkeypatch):
    from infrastructure.settings import Settings, settings

    monkeypatch.delenv("PUBLIC_FRONTEND_ORIGIN", raising=False)
    fallback = Settings(_env_file=None, DATABASE_URL="sqlite://", HASH_KEY="email-origin-test-only", APP_DOMAIN="WWW.Example.Test/path")
    assert fallback.PUBLIC_FRONTEND_ORIGIN == "https://www.example.test"
    monkeypatch.setattr(settings, "PUBLIC_FRONTEND_ORIGIN", fallback.PUBLIC_FRONTEND_ORIGIN)
    _, html_body, _ = render_template(EmailTemplateType.PASSWORD_RESET, name="Member", code="A1B2C3", expiry_minutes=15)
    images = re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', html_body)
    assert len(images) == 2
    assert all(src.startswith("https://www.example.test/") for src in images)
