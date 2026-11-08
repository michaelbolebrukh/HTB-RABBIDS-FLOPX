"""Durable private-plane ingest of SAFE PDF bytes only."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from platform.services.document_ingest.disarm import disarm_pdf_to_safe_bytes
from platform.services.document_ingest.sandbox import disarm_pdf_in_sandbox
from platform.services.storage import ObjectStorage, require_durable_storage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafePrivateObject:
    """What product may persist on a CvDocument (or similar) and later serve."""

    storage_key: str
    content_type: str
    size_bytes: int
    sha256: str
    sha256_original: str
    page_count: int


def ingest_private_safe_pdf(
    data: bytes,
    *,
    key_prefix: str,
    storage: Optional[ObjectStorage] = None,
    rebuild: Optional[Callable[[bytes], bytes]] = None,
) -> SafePrivateObject:
    """Validate → fail-closed rebuild (OG ephemeral) → `save_private(SAFE only)`.

    `key_prefix` is product-owned namespace, e.g. `users/{id}/cv`. Platform appends
    `/safe/{upload_id}.pdf` so share keys are visibly distinct from any future
    non-share prefix. Original bytes are never written to object storage.
    """
    import pypdfium2 as pdfium

    from platform.exceptions import DocumentDisarmFailedException

    sha_original = hashlib.sha256(data).hexdigest()

    # The parse happens in a separate, credential-free process by default (see
    # `sandbox.py`): this is the one place in the app that feeds an untrusted
    # file to a large C/C++ parser, and a memory bug there would otherwise run
    # as the worker. A `rebuild` override is a test seam — a Python callable
    # cannot cross a process boundary, so supplying one deliberately keeps the
    # work in-process, which is right for a test that is replacing the rebuild
    # step anyway.
    if rebuild is None:
        safe = disarm_pdf_in_sandbox(data)
    else:
        safe = disarm_pdf_to_safe_bytes(data, rebuild=rebuild)

    # Page count of the SAFE artefact (what partners receive):
    try:
        page_count = len(pdfium.PdfDocument(safe))
    except Exception:
        # Our OWN rebuild output failing to reopen is not a bad upload — it
        # means the disarm step produced something unreadable, which is worth
        # knowing about rather than silently folding into the same 422 a
        # corrupt upload gets. It still fails closed below; it just says why:
        logger.warning("Rebuilt safe PDF could not be reopened to count pages", exc_info=True)
        page_count = 0
    if page_count < 1:
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please export a new PDF and try again."
        )

    backend = storage if storage is not None else require_durable_storage("safe PDF ingest")
    prefix = key_prefix.strip().strip("/")
    if not prefix:
        raise ValueError("key_prefix must be a non-empty private-plane namespace")
    key = f"{prefix}/safe/{uuid4().hex}.pdf"
    backend.save_private(safe, key, "application/pdf")

    return SafePrivateObject(
        storage_key=key,
        content_type="application/pdf",
        size_bytes=len(safe),
        sha256=hashlib.sha256(safe).hexdigest(),
        sha256_original=sha_original,
        page_count=page_count,
    )
