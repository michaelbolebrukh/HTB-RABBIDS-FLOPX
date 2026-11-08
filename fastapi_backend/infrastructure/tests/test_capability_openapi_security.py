"""Every capability-gated operation is documented as needing a credential.

require_capability enforces authorization at request time whatever the schema
says, so a gate that stops declaring itself breaks nothing a status code can
see: 401, 403 and 200 all stay exactly as they were. What breaks is the
document. An operation with no `security` entry reads as anonymous to the docs
UI, to a generated client, and to anyone auditing the API from its schema — and
this went unnoticed for weeks across all 44 gated operations precisely because
no behavioural assertion can catch it, and nothing in this suite had ever looked
at app.openapi() at all.

So this asserts the documented contract and only that. The runtime matrix — no
credentials, grant held, grant missing, service token admitted or refused — is
covered against a real closure in test_capability_dependency.py and is not
repeated here.

Gated routes are enumerated from the real router graph rather than a list, so a
new gate is covered the day it lands, and the assertions stay agnostic about
WHICH scheme is declared: swapping schemes, or declaring a second one, must not
require editing this file.

The app is built in a SUBPROCESS. Importing main runs configure_logging() at
module scope, which mutates process-wide logging state and leaks into unrelated
test modules for the rest of the session (it breaks test_log_noise.py's
"nothing installed yet" baseline). Import caching means a fixture cannot undo
that — only a separate process can, and the child takes its logging config with
it when it exits.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


# Walks the real router graph and the real document in the child, returning only
# a small JSON verdict — no app object ever crosses back into the test process.
# The dependency tree is walked RECURSIVELY: a gate nested inside another
# dependency, or applied at router level, is still a gate:
_ANALYSIS = r"""
import json

import main
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from infrastructure.dependencies import require_capability
from platform.capabilities import USERS_READ

# Every require_capability(...) call mints a fresh closure, so gated routes are
# recognised by qualname rather than by identity. Any registered capability
# yields the same qualname; USERS_READ is used only because a real one must be
# passed — require_capability rejects strings absent from the registry:
GATE = require_capability(USERS_READ).__metadata__[0].dependency.__qualname__


def is_gated(dependant):
    for sub in dependant.dependencies:
        if getattr(getattr(sub, "call", None), "__qualname__", None) == GATE:
            return True
        if is_gated(sub):
            return True
    return False


gated = [
    (route.path, method.lower())
    for route in main.app.routes
    if isinstance(route, APIRoute) and is_gated(route.dependant)
    # FastAPI pairs HEAD with GET; it is not a separate document entry:
    for method in route.methods if method not in {"HEAD", "OPTIONS"}
]

schema = main.app.openapi()
declared = sorted(schema.get("components", {}).get("securitySchemes", {}))

undocumented, unknown_refs = [], []
for path, method in gated:
    operation = schema.get("paths", {}).get(path, {}).get(method, {})
    security = operation.get("security") or []
    if not security:
        undocumented.append(f"{method.upper()} {path}")
        continue
    for requirement in security:
        unknown = sorted(set(requirement) - set(declared))
        if unknown:
            unknown_refs.append(f"{method.upper()} {path} -> {unknown}")

# Same-named schemes are folded into ONE components entry by a last-writer-wins
# dict update, so two emitters that drifted apart (say, one tokenUrl edited and
# not the other) would publish whichever registered last, silently. Collect
# every SecurityBase in the dependency graph and require same-named ones to
# encode identically:
def collect_schemes(dependant, acc):
    for requirement in dependant.security_requirements:
        acc.append(requirement.security_scheme)
    for sub in dependant.dependencies:
        collect_schemes(sub, acc)


by_name = {}
for route in main.app.routes:
    if isinstance(route, APIRoute):
        schemes = []
        collect_schemes(route.dependant, schemes)
        for scheme in schemes:
            encoded = json.dumps(jsonable_encoder(scheme.model, exclude_none=True), sort_keys=True)
            by_name.setdefault(scheme.scheme_name, set()).add(encoded)

conflicting = sorted(name for name, models in by_name.items() if len(models) > 1)

print(json.dumps({
    "gated_count": len(gated),
    "declared": declared,
    "undocumented": sorted(undocumented),
    "unknown_refs": sorted(unknown_refs),
    "conflicting_schemes": conflicting,
}))
"""


@pytest.fixture(scope="module")
def report():
    """Runs the analysis against the real app, out of process.

    The child inherits this process's environment and only overrides what it
    needs, so conftest's test settings still apply — replacing the environment
    outright would strip PATH and anything else the interpreter expects. cwd is
    the backend root so the child resolves the `platform` package from the
    working directory the same way `python -m pytest` does:
    """
    child_env = {**os.environ, "DATABASE_URL": "sqlite://", "SENTRY_DSN": ""}
    result = subprocess.run(
        [sys.executable, "-c", _ANALYSIS],
        cwd=BACKEND_ROOT, env=child_env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"analysis subprocess failed:\n{result.stderr}"

    # Settings may emit warnings ahead of the verdict; the last stdout line is it:
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_capability_gated_routes_are_discoverable(report):
    """Guards the guard: at zero gated routes the real assertion passes vacuously"""
    assert report["gated_count"] > 0, "found no capability-gated routes — the detector is stale"


def test_every_capability_gated_operation_declares_security(report):
    """The regression itself: a gated operation documented as needing nothing"""
    assert report["declared"], "the document declares no security schemes at all"
    assert not report["unknown_refs"], (
        f"operations reference undeclared schemes: {report['unknown_refs']}"
    )
    assert not report["undocumented"], (
        f"{len(report['undocumented'])} capability-gated operations carry no security "
        f"requirement, so they read as anonymous to any client or auditor relying on "
        f"the document: {report['undocumented']}"
    )


def test_same_named_schemes_do_not_diverge(report):
    """Two emitters of one scheme name merge last-writer-wins, silently.

    The document can only hold one scheme per name, so if two SecurityBase
    instances share a name but not a model (a tokenUrl edited in one place and
    not the other), whichever registers last wins and the document misdescribes
    every operation that meant the other. Runtime behaviour would stay correct,
    which is exactly why nothing else can catch it."""
    assert not report["conflicting_schemes"], (
        f"security schemes emitted with one name but differing models: "
        f"{report['conflicting_schemes']}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
