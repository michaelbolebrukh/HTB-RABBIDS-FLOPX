"""Interim sink for the full-stack error snapshot — emails it to the admin.

============================================================================
TEMPORARY — this whole module is a placeholder sink and is meant to be REPLACED.
FIXME(ai-pipeline): the permanent design sends `snapshot` (the exact JSON emailed
below) to the custom AI diagnosis pipeline, which is being built separately. When
that pipeline exists, swap this admin email for a call into it (see the single
call site in service.py). Everything upstream — the webhook, verification, and
the request_id correlation that assembles `snapshot` — is permanent and does NOT
change; only this final delivery step does. Do not build product features on top
of the admin email; treat it as scaffolding.
============================================================================
"""

import json
import logging

from infrastructure.settings import settings as s
from platform.services.email_services.email_sender import send_email


logger = logging.getLogger(__name__)


def _recipient() -> str | None:
    # ADMIN_EMAIL may hold a CSV list; this single-recipient sink takes the
    # primary admin (first platform admin), falling back to CONTACT_EMAIL:
    return s.primary_admin_email or s.CONTACT_EMAIL


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_admin(snapshot: dict, ref: dict) -> None:
    """Emails the correlated full-stack snapshot to the admin. Raises on send
    failure so the caller can fail-open."""
    recipient = _recipient()
    if not recipient:
        logger.warning("Error snapshot ready but no ADMIN_EMAIL/CONTACT_EMAIL configured to notify")
        return

    payload = json.dumps(snapshot, indent=2, default=str)
    title = (snapshot.get("primary") or {}).get("title") or ref.get("title") or "error"
    tiers = ", ".join(snapshot.get("tiers_present") or []) or "unknown"
    request_id = snapshot.get("request_id")

    subject = f"[{s.APP_DOMAIN}] Full-stack error snapshot: {title}"
    text_body = (
        f"Full-stack error snapshot (this is the exact payload the AI pipeline will "
        f"receive once built).\n"
        f"Issue: {ref.get('issue_id')} · request_id: {request_id} · tiers: {tiers}\n\n"
        f"{payload}\n"
    )
    html_body = (
        f"<p><b>Full-stack error snapshot</b> — issue <code>{ref.get('issue_id')}</code>, "
        f"request_id <code>{request_id}</code>, tiers: {tiers}.</p>"
        f"<p>This is the exact payload the AI pipeline will receive once built.</p>"
        f"<pre style='white-space:pre-wrap;font-size:12px'>{_escape(payload)}</pre>"
    )
    send_email(recipient=recipient, subject=subject, html_body=html_body, text_body=text_body)
    logger.info("Sent full-stack error snapshot to admin for issue %s", ref.get("issue_id"))
