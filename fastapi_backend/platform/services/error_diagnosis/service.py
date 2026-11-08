"""Orchestrates the error-snapshot pipeline. Fully env-gated and fail-open: a
missing config makes it dormant, and any failure logs and stops without
affecting the request that delivered the webhook.

The final delivery step is TEMPORARY. Today it emails the correlated snapshot to
the admin (scaffolding). FIXME(ai-pipeline): the permanent design sends the
snapshot to the custom AI diagnosis pipeline being built separately — see the
clearly marked call site below and notifier.notify_admin. Only that one step
changes; the webhook, verification, and request_id correlation are permanent.
"""

import json
import logging

from infrastructure.settings import settings as s
from platform.services.error_diagnosis import sentry_gateway as gw
from platform.services.error_diagnosis.notifier import notify_admin
from platform.services.error_diagnosis.snapshot import build_snapshot


logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Runs only when the two Sentry credentials are configured. The org slug and
    the projects to correlate across are discovered from the token (see
    sentry_gateway), so only the webhook secret + read token are required."""
    return bool(s.SENTRY_WEBHOOK_SECRET and s.SENTRY_API_TOKEN)


def handle_sentry_webhook(body: bytes, signature: str | None) -> None:
    """Verify -> correlate by request_id -> output the snapshot. Safe to call
    from a background task; never raises."""
    if not is_enabled():
        return
    if not gw.verify_signature(body, signature):
        logger.warning("Rejected Sentry webhook: invalid or missing signature")
        return
    try:
        payload = json.loads(body)
    except ValueError:
        logger.warning("Sentry webhook body was not JSON")
        return
    ref = gw.parse_webhook(payload)
    if not ref:
        logger.info("Sentry webhook carried no actionable issue; ignoring")
        return

    try:
        primary = gw.get_latest_event(ref["issue_id"])
        request_id = ref.get("request_id") or _request_id_of(primary)
        correlated = (
            gw.find_events_by_request_id(request_id, ref.get("event_id"))
            if request_id else []
        )
        snapshot = build_snapshot(primary, correlated, request_id)
        # ====================================================================
        # TEMPORARY SINK — REPLACE WITH THE AI PIPELINE.
        # FIXME(ai-pipeline): `snapshot` is the finished, correlated payload. Today
        # it is emailed to the admin (scaffolding). The permanent design sends it
        # to the custom AI diagnosis pipeline being built separately — swap the
        # single line below for that call. Nothing above this line changes.
        # ====================================================================
        notify_admin(snapshot, ref)
        logger.info(
            "Delivered error snapshot for issue %s (request_id=%s, correlated=%d)",
            ref["issue_id"], request_id, len(correlated),
        )
    except Exception:
        # Fail-open: collection is best-effort and must never disrupt anything:
        logger.exception("Error-snapshot pipeline failed for issue %s", ref.get("issue_id"))


def _request_id_of(event: dict) -> str | None:
    tags = event.get("tags")
    if isinstance(tags, list):
        return {t.get("key"): t.get("value") for t in tags if isinstance(t, dict)}.get("request_id")
    return (tags or {}).get("request_id")
