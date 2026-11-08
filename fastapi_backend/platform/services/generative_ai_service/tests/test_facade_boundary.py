"""
Facade-boundary guards: `pydantic_ai` may be imported ONLY in
engine/adapter.py and engine/providers.py (D3), and provider realtime/audio
SDKs (websockets, google-genai, the raw provider HTTP surfaces) ONLY in the
voice adapters (D39). These tests grep the package source and fail the build
if a boundary is breached — the single most important structural invariant of
the whole design.
"""

import pathlib

import pytest


PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOWED = {"engine/adapter.py", "engine/providers.py"}

# The D39 voice seams — the only modules allowed to speak provider realtime/
# audio wire formats (websockets relay, google-genai live SDK):
VOICE_SEAMS = {
    "voice/adapters/openai_realtime.py",
    "voice/adapters/gemini_live.py",
    "voice/adapters/pipeline.py",
}
_PROVIDER_SDK_PREFIXES = (
    "import websockets", "from websockets",
    "import openai", "from openai",
    "from google import genai", "from google.genai", "import google.genai",
)


def _python_files():
    for path in PACKAGE_ROOT.rglob("*.py"):
        # Skipping tests (they legitimately import pydantic_ai to build TestModels)
        # and the manual benchmark harnesses (offline tooling, never imported by
        # the service — they call provider SDKs directly to drive live runs):
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        if rel.startswith("tests/") or rel.startswith("benchmarks/"):
            continue
        yield rel, path


def test_pydantic_ai_import_confined_to_adapter_and_providers():
    offenders = []
    for rel, path in _python_files():
        if rel in ALLOWED:
            continue
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import pydantic_ai", "from pydantic_ai")):
                offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "pydantic_ai imported outside the facade boundary (D3):\n  " + "\n  ".join(offenders)
    )


def test_provider_sdk_imports_confined_to_voice_adapters():
    """The D39 quarantine: no provider realtime/audio SDK outside the sanctioned seams."""
    offenders = []
    for rel, path in _python_files():
        if rel in VOICE_SEAMS:
            continue
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(_PROVIDER_SDK_PREFIXES):
                offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, (
        "provider SDK imported outside the voice-adapter seams (D39):\n  " + "\n  ".join(offenders)
    )
