import html
import os
import traceback
import logging
from pathlib import Path
from typing import Tuple, Optional
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

from infrastructure.settings import settings
from platform.exceptions import EmailSendException
from platform.enums import EmailTemplateType
from . import email_sender
# Re-exported so callers gate on the sender's own notion of "a provider is
# wired" instead of re-deriving it from settings:
from .email_sender import provider_is_configured  # noqa: F401


# Setting up logger:
logger = logging.getLogger(__name__)

# Template directory path:
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Initializing Jinja2 environment:
jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(
        enabled_extensions=('html', 'xml'),
        disabled_extensions=('txt',),
        default_for_string=True,
        default=False
    ),
    trim_blocks=True,
    lstrip_blocks=True
)


def assert_templates_exist() -> None:
    """Verifies that all required email templates exist on disk"""
    # Checking for required templates:
    missing_templates = []
    
    for template_type in EmailTemplateType:
        html_file = TEMPLATES_DIR / f"{template_type.value}.html"
        txt_file = TEMPLATES_DIR / f"{template_type.value}.txt"
        
        if not html_file.exists():
            missing_templates.append(f"{template_type.value}.html")
        if not txt_file.exists():
            missing_templates.append(f"{template_type.value}.txt")
    
    if missing_templates:
        raise FileNotFoundError(
            f"Required email templates are missing: {', '.join(missing_templates)}. "
            f"Please run the email build script to generate templates."
        )


def send_templated_email(
    template_type: EmailTemplateType,
    recipient: str,
    reply_to: Optional[str] = None,
    headers: Optional[dict] = None,
    tags: Optional[list] = None,
    subject_override: Optional[str] = None,
    **variables,
) -> None:
    """Sends an email using a Jinja2 template with provided variables"""
    # Rendering template with variables:
    subject, html_body, plaintext_body = render_template(template_type, **variables)
    # A caller-supplied subject (e.g. the contact form's subject field) replaces
    # the template's generic one without a template rebuild:
    if subject_override:
        subject = subject_override

    # Sending email using rendered template:
    email_sender.send_email(
        recipient=recipient,
        subject=subject,
        html_body=html_body,
        text_body=plaintext_body,
        reply_to=reply_to,
        headers=headers,
        tags=tags,
    )


def render_template(template_type: EmailTemplateType, **variables) -> Tuple[str, str, str]:
    """Renders email template and extracts subject, HTML body, and text body"""
    try:
        # Loading HTML and text templates:
        html_template = jinja_env.get_template(f"{template_type.value}.html")
        txt_template = jinja_env.get_template(f"{template_type.value}.txt")
        
        # Local brand images belong to this deployment. Reserve the origin so callers
        # cannot alter it; never rewrite recipient fields or links after interpolation.
        render_context = {**variables, "email_asset_origin": settings.PUBLIC_FRONTEND_ORIGIN}
        html_body = html_template.render(**render_context)
        plaintext_body = txt_template.render(**render_context)
        
        # Extracting subject from HTML template. The subject line is rendered
        # inside the AUTOESCAPED html, but an email Subject header is plain
        # text, so the entity escaping is undone here - otherwise a subject
        # containing "&" arrives as "&amp;":
        html_lines = html_body.strip().split('\n')
        if html_lines and html_lines[0].startswith('Subject:'):
            subject = html.unescape(html_lines[0].replace('Subject:', '').strip())
            html_body = '\n'.join(html_lines[1:]).strip()
        else:
            subject = "Email Notification"
        
        # Applying same subject stripping logic to plaintext template:
        plaintext_lines = plaintext_body.strip().split('\n')
        if plaintext_lines and plaintext_lines[0].startswith('Subject:'):
            plaintext_body = '\n'.join(plaintext_lines[1:]).strip()
        
        return subject, html_body, plaintext_body
        
    except TemplateNotFound:
        logger.error(f"Template not found: {template_type.value}")
        raise EmailSendException(f"Email template '{template_type.value}' not found")
    
    except Exception as e:
        logger.error(f"Template rendering failed for {template_type.value}: {e}")
        traceback.print_exc()
        raise EmailSendException(f"Failed to render email template '{template_type.value}'")


# Asserting templates exist on import:
assert_templates_exist()
