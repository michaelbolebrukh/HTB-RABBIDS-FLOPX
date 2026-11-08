"""
Suite integrity — a test may not encode a known defect as expected behaviour.

This repo has repeatedly used the test suite to DOCUMENT holes instead of
closing them: an `xfail` named "voice WS never verifies conversation ownership"
that was marking a live cross-user IDOR, a test asserting the `isAdmin`
behaviour of an access-control violation (pinning the violation so the correct
implementation would fail), a guard test deleted and replaced by tests
asserting the opposite property, an `xfail` for a non-JSON media response that
crashed runs untyped. Each was invisible in a green run.

So the rule is enforced, not just written down:

* **`xfail` is banned outright.** An expected failure is a known defect the
  suite has agreed to stop reporting. If it cannot be fixed now, the failure
  must stay VISIBLE — leave the test failing, or delete it and open the issue.
* **`skip` / `skipif` must gate on an ENVIRONMENT precondition**, never on a
  defect: no docker daemon, no Postgres, no opt-in integration key, a file
  absent from this checkout. Every one carries a `reason` saying which.
* The same ban applies to the frontend suites (`.skip` / `.todo` / `.failing`),
  which have no environment gating at all today.

Adding a legitimate environment-gated skip needs nothing but a `reason` that
does not read as a defect. Adding an `xfail` needs this test changed on purpose,
in review — which is exactly the friction that was missing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _REPO_ROOT / "fastapi_backend"
_FRONTEND = _REPO_ROOT / "next-pwa"
_SELF = Path(__file__).resolve()

# Words that turn a skip reason into "we know this is broken". A skip may name
# a missing service, key, or file — never a defect in our own code:
_DEFECT_WORDS = re.compile(
    r"\b(fixme|todo|bug|broken|flaky|fails?|failing|known issue|"
    r"not implemented|unimplemented|regress\w*|wip)\b",
    re.IGNORECASE,
)


def _backend_test_files() -> list[Path]:
    return [
        path
        for path in _BACKEND.rglob("test_*.py")
        if ".venv" not in path.parts and path.resolve() != _SELF
    ]


def _marker_name(node: ast.expr) -> str | None:
    """The dotted tail of a decorator/call, e.g. `pytest.mark.xfail` -> 'xfail'."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    dotted = ".".join(reversed(parts))
    if dotted.startswith("pytest.mark.") or dotted.startswith("pytest.skip"):
        return dotted.split(".")[-1]
    return None


def _reason_of(node: ast.expr) -> str | None:
    """The `reason=` text (or first positional, for `pytest.skip("…")`)."""
    if not isinstance(node, ast.Call):
        return None
    for kw in node.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _markers() -> list[tuple[Path, int, str, str | None]]:
    """Every xfail/skip/skipif site in the backend suite: (file, line, kind, reason)."""
    found: list[tuple[Path, int, str, str | None]] = []
    for path in _backend_test_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — a broken file fails elsewhere, loudly
            continue
        for node in ast.walk(tree):
            candidates: list[ast.expr] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                candidates = list(node.decorator_list)
            elif isinstance(node, ast.Call):
                candidates = [node]
            elif isinstance(node, ast.Assign):
                candidates = [node.value]
            for candidate in candidates:
                kind = _marker_name(candidate)
                if kind in {"xfail", "skip", "skipif"}:
                    found.append((path, candidate.lineno, kind, _reason_of(candidate)))
    # A `pytestmark = pytest.mark.skipif(…)` assignment is reached both as an
    # Assign and as a Call — one site, one report:
    return sorted(set(found), key=lambda row: (str(row[0]), row[1]))


def test_no_backend_xfail_anywhere():
    """An expected failure is a known defect the suite stopped reporting."""
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{line} — {reason!r}"
        for path, line, kind, reason in _markers()
        if kind == "xfail"
    ]
    assert not offenders, (
        "xfail is banned: a test may not encode a known defect as expected behaviour. "
        "Fix the defect, or leave the failure visible.\n  " + "\n  ".join(offenders)
    )


def test_every_backend_skip_gates_on_the_environment_with_a_reason():
    """Skips are for absent services/keys/files — never for our own broken code."""
    missing: list[str] = []
    defective: list[str] = []
    for path, line, kind, reason in _markers():
        if kind == "xfail":
            continue
        where = f"{path.relative_to(_REPO_ROOT)}:{line}"
        if not reason or not reason.strip():
            missing.append(f"{where} — {kind} with no reason")
        elif _DEFECT_WORDS.search(reason):
            defective.append(f"{where} — {kind}: {reason!r}")

    assert not missing, (
        "every skip/skipif needs a reason naming the environment precondition it "
        "gates on:\n  " + "\n  ".join(missing)
    )
    assert not defective, (
        "a skip may not be justified by a defect — that hides a real failure. "
        "Fix it, or let it fail:\n  " + "\n  ".join(defective)
    )


# The frontend suites are plain scripts with no environment gating, so any
# disabled test there is unambiguously a hidden failure:
_JS_DISABLERS = re.compile(r"\b(?:it|test|describe)\.(?:skip|todo|failing)\b")


@pytest.mark.skipif(not _FRONTEND.is_dir(), reason="next-pwa not present in this checkout")
def test_no_disabled_frontend_tests():
    offenders: list[str] = []
    for pattern in ("*.test.ts", "*.test.tsx"):
        for path in _FRONTEND.rglob(pattern):
            if "node_modules" in path.parts:
                continue
            for number, text in enumerate(path.read_text().splitlines(), start=1):
                if _JS_DISABLERS.search(text):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number} — {text.strip()}")
    assert not offenders, (
        "disabled frontend tests hide failures — fix or delete them:\n  "
        + "\n  ".join(offenders)
    )
