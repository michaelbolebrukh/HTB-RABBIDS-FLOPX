"""Reduce raw Sentry events into a compact, model-friendly full-stack snapshot.

Sentry event JSON is large and deeply nested; this pulls the parts that matter
for diagnosis (exception + stack + frame locals, the breadcrumb trail, request,
and the correlating tags) and trims the rest so the payload stays small.
"""

from typing import Any


def _entries(event: dict) -> dict[str, Any]:
    return {e.get("type"): e.get("data") for e in (event.get("entries") or [])}


def _tags(event: dict) -> dict[str, str]:
    tags = event.get("tags")
    if isinstance(tags, list):
        return {t.get("key"): t.get("value") for t in tags if isinstance(t, dict)}
    return dict(tags or {})


def _frames(stacktrace: dict, keep: int = 12) -> list[dict]:
    frames = (stacktrace or {}).get("frames") or []
    # Sentry lists frames oldest-first; the crash is last. Prefer in-app frames:
    picked = [f for f in frames if f.get("inApp")] or frames
    out = []
    for f in picked[-keep:]:
        out.append({
            "function": f.get("function"),
            "file": f.get("filename") or f.get("absPath"),
            "line": f.get("lineNo"),
            "in_app": f.get("inApp"),
            # Frame locals are the gold for backend bugs; trim to keep it small:
            "vars": _trim_vars(f.get("vars")),
        })
    return out


def _trim_vars(vars_: Any, limit: int = 12) -> Any:
    if not isinstance(vars_, dict):
        return None
    out = {}
    for i, (k, v) in enumerate(vars_.items()):
        if i >= limit:
            break
        sv = str(v)
        out[k] = sv[:200] + "…" if len(sv) > 200 else sv
    return out or None


def summarize_event(event: dict) -> dict:
    entries = _entries(event)
    tags = _tags(event)

    exc = None
    exc_data = entries.get("exception")
    if isinstance(exc_data, dict) and exc_data.get("values"):
        v = exc_data["values"][-1]
        exc = {"type": v.get("type"), "value": v.get("value"),
               "frames": _frames(v.get("stacktrace"))}

    breadcrumbs = []
    bc = entries.get("breadcrumbs")
    if isinstance(bc, dict):
        for c in (bc.get("values") or [])[-15:]:
            breadcrumbs.append({"category": c.get("category"), "level": c.get("level"),
                                "message": c.get("message"),
                                "data": _trim_vars(c.get("data"), limit=8)})

    request = None
    req = entries.get("request")
    if isinstance(req, dict):
        request = {"method": req.get("method"), "url": req.get("url")}

    return {
        "tier": tags.get("component") or event.get("platform"),
        "title": event.get("title"),
        "environment": tags.get("environment"),
        "release": tags.get("release"),
        "request_id": tags.get("request_id"),
        "transaction": tags.get("transaction") or event.get("culprit"),
        "runtime": tags.get("runtime"),
        "user_id": (event.get("user") or {}).get("id"),
        "exception": exc,
        "request": request,
        "breadcrumbs": breadcrumbs,
    }


def build_snapshot(primary: dict, correlated: list[dict], request_id: str | None) -> dict:
    """A compact full-stack view: the triggering event plus every event that
    shares its request_id, each summarized."""
    return {
        "request_id": request_id,
        "primary": summarize_event(primary),
        "correlated": [summarize_event(e) for e in correlated],
        "tiers_present": sorted({
            s for s in [summarize_event(primary).get("tier")]
            + [summarize_event(e).get("tier") for e in correlated] if s
        }),
    }
