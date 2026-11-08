"""Platform safe-document ingest: ephemeral OG → allowlist rebuild → private share.

Product code that must store a member-uploaded PDF for later download MUST use
`ingest_private_safe_pdf` (or `disarm_pdf_to_safe_bytes` then its own durable
Put of the returned safe bytes). Never call `save_private` with as-received
upload bytes, and never use `document_sanitize.sanitize_pdf` as a share-plane
gate — that helper fails open by design and is not a CDR path.

The names below resolve on first use rather than at package import, for one
specific reason: `ingest` reaches object storage and therefore `Settings()`,
while the disarm ladder itself (`disarm`, `rebuild`, `textlayer`) needs nothing
but stdlib, pikepdf, PDFium and Pillow. Binding them eagerly would mean the
sandbox worker — a separate process with a scrubbed environment and no
credentials — could not import the ladder without a DATABASE_URL it is not
supposed to have. Keeping this lazy is what lets the parser run somewhere that
holds no secrets. See `sandbox.py`.
"""

from importlib import import_module
from typing import TYPE_CHECKING

# Exported name -> the submodule that defines it.
_LAZY_EXPORTS = {
    "disarm_pdf_to_safe_bytes": "disarm",
    "rebuild_limiter": "disarm",
    "SafePrivateObject": "ingest",
    "ingest_private_safe_pdf": "ingest",
    "disarm_pdf_in_sandbox": "sandbox",
}

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from .disarm import disarm_pdf_to_safe_bytes, rebuild_limiter
    from .ingest import SafePrivateObject, ingest_private_safe_pdf
    from .sandbox import disarm_pdf_in_sandbox


def __getattr__(name: str):
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{submodule}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "SafePrivateObject",
    "disarm_pdf_in_sandbox",
    "disarm_pdf_to_safe_bytes",
    "ingest_private_safe_pdf",
    "rebuild_limiter",
]
