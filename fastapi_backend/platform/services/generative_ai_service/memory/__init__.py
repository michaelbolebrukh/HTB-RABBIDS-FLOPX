"""
Memory — durable, bi-temporal, agent-authored fact store (Phase 10).

A fact store with valid-time + transaction-time (Graphiti's model, ported onto
our portable Postgres/JSON-embedding stack), write-time reconciliation
(supersede-vs-accrue on timestamps — the part mem0/cognee/Letta lack), and a
self-editing tool surface (Letta docstrings). See MEMORY.md for the
mechanism-by-mechanism frontier provenance.

Importing this package registers the four memory tools into the tool table
(D33); the engine exposes them per-agent behind `features.memory`.
"""

from __future__ import annotations

from .extract import (
    EXTRACT_INSTRUCTIONS,
    build_extract_prompt,
    parse_extract_response,
)
from .reconcile import (
    ContradictionOutcome,
    EmbedFn,
    Invalidation,
    ReconcileResult,
    ResolveFn,
    ResolveResult,
    build_resolve_prompt,
    parse_resolve_response,
    reconcile_write,
    temporal_resolve_contradictions,
    RESOLVE_INSTRUCTIONS,
)
from .store import (
    add_memory,
    content_hash,
    cosine,
    live_memories,
    normalize_text,
    recall,
    rrf_fuse,
)
from .tools import (
    MEMORY_PREAMBLE,
    MEMORY_TOOL_NAMES,
    memory_delete,
    memory_search,
    memory_supersede,
    memory_write,
)


__all__ = [
    # Reconciliation (write path):
    "reconcile_write",
    "ReconcileResult",
    "ResolveResult",
    "ResolveFn",
    "EmbedFn",
    "temporal_resolve_contradictions",
    "ContradictionOutcome",
    "Invalidation",
    "build_resolve_prompt",
    "parse_resolve_response",
    "RESOLVE_INSTRUCTIONS",
    "EXTRACT_INSTRUCTIONS",
    "build_extract_prompt",
    "parse_extract_response",
    # Store (DB + ranking):
    "add_memory",
    "recall",
    "live_memories",
    "content_hash",
    "normalize_text",
    "cosine",
    "rrf_fuse",
    # Tools + engine hooks:
    "memory_write",
    "memory_search",
    "memory_supersede",
    "memory_delete",
    "MEMORY_TOOL_NAMES",
    "MEMORY_PREAMBLE",
]
