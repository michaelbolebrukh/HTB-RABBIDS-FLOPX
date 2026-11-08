"""Full-stack error-snapshot pipeline (fully env-gated platform capability).

A Sentry issue webhook arrives -> we verify it -> correlate the frontend and
backend events by the shared `request_id` (the correlation the observability
layer already stamps) -> assemble a compact full-stack snapshot -> emit it.

The emit step is TEMPORARY: it currently emails the snapshot to the admin as
scaffolding. FIXME(ai-pipeline): the permanent design sends the snapshot to the
custom AI diagnosis pipeline (built separately). When it exists, swap the sink
in service.py (single, clearly marked call site) / notifier.notify_admin. The
collection + correlation upstream are permanent and do not change.

Dormant unless `SENTRY_WEBHOOK_SECRET` and `SENTRY_API_TOKEN` are set (the org and
projects are discovered from the token). Fail-open: any step failing logs and
stops, never affecting request handling.
"""

from platform.services.error_diagnosis.service import handle_sentry_webhook, is_enabled

__all__ = ["handle_sentry_webhook", "is_enabled"]
