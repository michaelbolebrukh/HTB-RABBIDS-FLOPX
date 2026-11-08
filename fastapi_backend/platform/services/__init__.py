"""Platform services.

Submodules load ON FIRST USE rather than at package import, and the difference
is not cosmetic. Importing anything under `platform.services` used to pull in
`auth_services`, which reaches `platform.models`, which constructs `Settings()`
— and, further down, the whole generative-AI SDK stack (`pydantic_ai`,
`anthropic`, `openai`, `google.genai`, `mcp`). Measured on this checkout, that
chain costs 3.7-4.5 SECONDS of import time and a large fixed slice of every
worker's resident memory, whether or not the request touches AI at all. It is
the "eager per-worker footprint" the render ledger records as the reason
production was moved off the starter tier after an out-of-memory restart at
fewer than ten active users.

Two things depend on this staying lazy:

- The document-ingest sandbox runs the PDF disarm ladder in a separate process
  with a scrubbed environment and no credentials. That child imports
  `platform.services.document_ingest.disarm`, so an eager package import would
  drag settings (and therefore a required DATABASE_URL) into a process that
  deliberately has none, and add seconds to every upload.
- Boot time and per-worker memory for the app itself.

`from platform.services import user_service` and `platform.services.user_service`
both still work — PEP 562 module `__getattr__` resolves the submodule on first
attribute access and caches it in the module globals, so the second lookup is a
plain dict hit. Import the submodule directly when you want it eagerly.
"""

from importlib import import_module
from typing import TYPE_CHECKING

_LAZY_SUBMODULES = frozenset(
    {"auth_services", "contact_service", "email_services", "user_service"}
)

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from . import auth_services, contact_service, email_services, user_service


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_SUBMODULES)
