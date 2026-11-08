"""Sentry API access for the snapshot pipeline: verify inbound webhooks and read
the triggering + correlated events.

All calls use `SENTRY_API_TOKEN` (read-only scope is sufficient); nothing here
runs unless the feature is configured (checked by the caller).
"""

import hashlib
import hmac
import logging

import httpx

from infrastructure.constant_time import constant_time_equals
from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)

_TIMEOUT = 15


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 of the raw body against the webhook secret (constant-time)."""
    if not s.SENTRY_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(s.SENTRY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return constant_time_equals(expected, signature)


def _headers() -> dict:
    return {"Authorization": f"Bearer {s.SENTRY_API_TOKEN}"}


# Org + projects are DISCOVERED from the API token, not hand-set (config-minimalism):
# an internal-integration token belongs to exactly one org, whose projects are
# listable. `SENTRY_ORG_SLUG` / `SENTRY_PROJECT_SLUGS` remain optional overrides for
# the edge case of a token that reaches several orgs/projects; unset = auto-discover.
_org_slug_cache: str | None = None
_project_slugs_cache: list[str] | None = None


def reset_discovery_cache() -> None:
    """Clears the memoized org/project discovery (used by tests)."""
    global _org_slug_cache, _project_slugs_cache
    _org_slug_cache = None
    _project_slugs_cache = None


def _resolve_org_slug() -> str:
    """The org slug: `SENTRY_ORG_SLUG` if set, else discovered from the API token
    (an internal-integration token belongs to exactly one org). Memoized."""
    global _org_slug_cache
    if s.SENTRY_ORG_SLUG:
        return s.SENTRY_ORG_SLUG
    if _org_slug_cache is not None:
        return _org_slug_cache
    url = f"{s.SENTRY_API_BASE}/api/0/organizations/"
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url, headers=_headers())
        r.raise_for_status()
        slugs = [o.get("slug") for o in r.json() if o.get("slug")]
    if len(slugs) != 1:
        # Fail with an explicit ask rather than guessing among candidates:
        raise RuntimeError(
            f"Cannot auto-resolve the Sentry org: the API token sees {len(slugs)} "
            "organizations. Set SENTRY_ORG_SLUG to disambiguate."
        )
    _org_slug_cache = slugs[0]
    return _org_slug_cache


def _resolve_project_slugs() -> list[str]:
    """Projects to correlate across: `SENTRY_PROJECT_SLUGS` (csv) if set, else every
    project the token can see in the org. Memoized."""
    global _project_slugs_cache
    override = [p.strip() for p in (s.SENTRY_PROJECT_SLUGS or "").split(",") if p.strip()]
    if override:
        return override
    if _project_slugs_cache is not None:
        return _project_slugs_cache
    org = _resolve_org_slug()
    url = f"{s.SENTRY_API_BASE}/api/0/organizations/{org}/projects/"
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url, headers=_headers())
        r.raise_for_status()
        _project_slugs_cache = [p.get("slug") for p in r.json() if p.get("slug")]
    return _project_slugs_cache


def parse_webhook(payload: dict) -> dict | None:
    """Extracts the issue id and (if present) the request_id tag from a Sentry
    issue/event webhook payload. Returns None if there is no issue to act on."""
    data = payload.get("data", {}) or {}
    event = data.get("event", {}) or {}
    issue = data.get("issue", {}) or {}
    issue_id = event.get("issue_id") or issue.get("id")
    if not issue_id:
        return None
    tags = dict(event.get("tags", []) or [])
    return {
        "issue_id": str(issue_id),
        "event_id": event.get("event_id"),
        "request_id": tags.get("request_id"),
        "component": tags.get("component"),
        "title": event.get("title") or issue.get("title"),
    }


def get_latest_event(issue_id: str) -> dict:
    """Full detail of the issue's latest event (org-scoped; no project slug needed)."""
    org = _resolve_org_slug()
    url = f"{s.SENTRY_API_BASE}/api/0/organizations/{org}/issues/{issue_id}/events/latest/"
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url, headers=_headers())
        r.raise_for_status()
        return r.json()


def find_events_by_request_id(request_id: str, exclude_event_id: str | None) -> list[dict]:
    """Full events (across the configured projects) that share the request_id tag,
    excluding the triggering event. This is what stitches the two tiers together."""
    found: list[dict] = []
    org = _resolve_org_slug()
    for project in _resolve_project_slugs():
        url = f"{s.SENTRY_API_BASE}/api/0/projects/{org}/{project}/events/"
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.get(url, headers=_headers(),
                          params={"query": f"request_id:{request_id}", "full": "true"})
                r.raise_for_status()
                for ev in r.json():
                    if ev.get("eventID") != exclude_event_id and ev.get("id") != exclude_event_id:
                        found.append(ev)
        except httpx.HTTPError as e:
            logger.warning("Correlation search failed for project %s: %s", project, e)
    return found
