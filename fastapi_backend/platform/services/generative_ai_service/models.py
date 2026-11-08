"""
AI transcript tables — package-owned schema (Phase 1, D26 extraction rules).

These tables deliberately live INSIDE this package rather than in the host
backend's `platform/models.py`, because this service will eventually be
extracted into its own web service that multiple consumers plug into:

- Every table is prefixed `ai_` so the whole schema lifts out as a namespace.
- `owner_id` is a plain string with NO foreign key into the host's `users`
  table — each consumer maps its own identity onto it (QUANTSOC passes
  `str(user.id)`).
- The only host couplings are `infrastructure.database.Base` (so the tables
  join the host's metadata/migration machinery) and the portable column types
  in `infrastructure/db_types.py`. Both are documented, injected-style
  couplings to be replaced trivially at extraction time.

Append-only contract: `ai_transcript_items` rows are never UPDATEd after
insert (except the conversation's `current_item_id` pointer, which lives on
`ai_conversations`). Editing a message = inserting a sibling branch. The only
DELETE the store exposes is conversation-level hard delete (privacy), which
cascades to items and summaries.
"""

from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func

from infrastructure.database import Base
from infrastructure.db_types import JSONColumn, Uuid


class AIConversation(Base):
    """One conversation: an owner, a title, and a pointer to the active leaf."""
    __tablename__ = "ai_conversations"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    # Host-agnostic owner identity (D26) — indexed for per-owner listing, no FK:
    owner_id = Column(String, nullable=False, index=True)

    title = Column(String, nullable=True)

    # The active branch's leaf item; the store maintains this on every append/branch.
    # Not a ForeignKey to avoid a circular insert dependency between the two tables:
    current_item_id = Column(Uuid(as_uuid=True), nullable=True)

    meta = Column(JSONColumn, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AITranscriptItem(Base):
    """
    One transcript item (see transcript/types.py for the field semantics and
    the two-lane rendering invariant). Column set mirrors the pydantic `Item`
    one-to-one so (de)serialization stays a dumb field copy.
    """
    __tablename__ = "ai_transcript_items"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Branching: None = root; several roots/siblings may exist (edit-as-branch):
    parent_id = Column(Uuid(as_uuid=True), ForeignKey("ai_transcript_items.id"), nullable=True, index=True)

    # Monotonic per-conversation insertion order. Server timestamps are NOT safe
    # for ordering — multiple items appended in one run share a `created_at`
    # (SQLite second-resolution; PostgreSQL now() returns transaction-start time),
    # so a random-UUID tiebreak would scramble intra-turn order. The store
    # assigns `seq = max(seq)+1` per conversation at append; full_history orders
    # by it. Portable across SQLite/PostgreSQL (plain integer, app-assigned):
    seq = Column(Integer, nullable=False, default=0, index=True)

    # ItemKind/Role enum `.value` strings (repo enum rule — portable, readable migrations):
    kind = Column(String, nullable=False, index=True)
    role = Column(String, nullable=True)

    # Universal content — list of ContentPart dicts (see types.content_parts_adapter):
    parts = Column(JSONColumn, nullable=False, default=list)

    # Tool linkage (tool_call / tool_result kinds only — enforced in the pydantic layer):
    tool_name = Column(String, nullable=True)
    tool_call_id = Column(String, nullable=True)

    # Which model produced this item (None = not model-generated, e.g. user messages):
    model_used = Column(String, nullable=True)

    # Provider-reported usage dict and our per-tokenizer-family estimate cache:
    usage = Column(JSONColumn, nullable=True)
    token_estimates = Column(JSONColumn, nullable=False, default=dict)

    # Fast-lane framework payload (two-lane invariant — never parsed by readers):
    raw = Column(JSONColumn, nullable=True)

    meta = Column(JSONColumn, nullable=False, default=dict)

    # Extracted searchable text (TextParts + media transcripts) for keyless search:
    text_content = Column(Text, nullable=True)

    # Optional embedding vector (list[float]) — flag-gated search v2; portable JSON,
    # ranked in Python per conversation (no pgvector dependency, per PHASE1.md):
    embedding = Column(JSONColumn, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class AITranscriptSummary(Base):
    """
    Summary CACHE entries — never transcript content (D4/D24). A row means:
    "the span from the conversation's roots up to and including
    `span_end_item_id`, compressed to <= `token_budget` tokens, for models in
    `tokenizer_family`". The P2 renderer looks these up instead of
    re-summarizing; different models with different windows produce different
    cache rows for the same span (that is the conditional-summarization design).
    """
    __tablename__ = "ai_transcript_summaries"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The boundary item the summary covers up to (legacy "summary on the boundary node" idea):
    span_end_item_id = Column(Uuid(as_uuid=True), nullable=False)

    token_budget = Column(Integer, nullable=False)
    tokenizer_family = Column(String, nullable=False)
    summary_text = Column(Text, nullable=False)

    # What producing this summary cost (provider usage dict) — feeds cost accounting:
    usage = Column(JSONColumn, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # One cache entry per (span, budget, tokenizer family) — the renderer's lookup key:
        UniqueConstraint("conversation_id", "span_end_item_id", "token_budget", "tokenizer_family"),
        Index("ix_ai_summaries_lookup", "conversation_id", "span_end_item_id"),
    )


class AISpendEvent(Base):
    """
    Append-only spend ledger (Phase 4, D7): one row per priced unit of work —
    a model run, a summarization, later an embedding batch. Costs are stored
    as INTEGER MICRO-USD (1_000_000 = $1.00): exact, portable, fast to SUM.
    `priced=False` rows carry the pessimistic-policy cost for unpriced models
    (still counted against budgets so unknown models can't spend invisibly).
    """
    __tablename__ = "ai_spend_events"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    # Host-agnostic owner identity (D26) — the primary budget scope:
    owner_id = Column(String, nullable=False)
    conversation_id = Column(Uuid(as_uuid=True), nullable=True, index=True)
    run_id = Column(Uuid(as_uuid=True), nullable=True)
    agent_name = Column(String, nullable=True)

    # The model that ACTUALLY served (post-fallback) — fallback traffic is
    # billed at OpenRouter's rates, so attribution must follow the real lane:
    model_id = Column(String, nullable=False)

    kind = Column(String, nullable=False, default="run")   # run | summarization | embedding
    usage = Column(JSONColumn, nullable=False, default=dict)
    cost_microusd = Column(BigInteger, nullable=True)
    priced = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The window-sum hot path (P4 pre-flight) scans (owner, time):
        Index("ix_ai_spend_owner_time", "owner_id", "created_at"),
    )


class AIBudget(Base):
    """
    A spending limit for a scope over a rolling window (Phase 4). Multiple
    windows may coexist for one scope (e.g. $2/day AND $20/month — both are
    enforced, strictest remaining wins). A missing row falls back to the
    settings-level defaults; an explicit row always wins.
    """
    __tablename__ = "ai_budgets"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    scope_type = Column(String, nullable=False)             # owner | conversation | agent | global
    scope_id = Column(String, nullable=False, index=True)   # owner_id / conversation uuid / agent name / "*"
    window = Column(String, nullable=False)                 # daily | weekly | monthly | total
    limit_microusd = Column(BigInteger, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)

    meta = Column(JSONColumn, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        # One row per scope x window — updates edit the row, never duplicate it:
        UniqueConstraint("scope_type", "scope_id", "window"),
    )


class AIUserExtension(Base):
    """
    A user-added MCP server or Skill bound to one agent (Phase 9, D51). Only
    loaded at run time when that agent's `allow_user_mcp` / `allow_user_skills`
    toggle is on (the developer of the user-facing product controls the toggle).
    Scoped to `owner_id`: never visible to other users of the same product —
    cross-user visibility is the host product's concern, not this service's
    (the AI-service-is-the-product principle, D54).
    """
    __tablename__ = "ai_user_extensions"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    owner_id = Column(String, nullable=False)          # The user who added it (budget + isolation scope)
    agent_name = Column(String, nullable=False)         # The agent it is bound to
    kind = Column(String, nullable=False)               # mcp | skill

    # mcp: {"url": ..., "headers": {...}} (remote-only, token-paste).
    # skill: {"skill_md": ..., "files": {name: text}}. No secrets beyond the
    # user's own token, which they entrusted for their own server:
    config = Column(JSONColumn, nullable=False, default=dict)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The run-time load path scans (owner, agent):
        Index("ix_ai_user_ext_owner_agent", "owner_id", "agent_name"),
    )


class AIMemory(Base):
    """
    One agent-authored durable fact — a bi-temporal fact store (P10), ported
    from Graphiti's EntityEdge model onto our portable stack (JSON embedding,
    Python-side cosine; no pgvector dependency, like the transcript). Facts are
    never physically deleted on contradiction — they are INVALIDATED (the
    "invalidate, don't delete" rule), so "what is true now", "what was true at
    date T" (valid-time travel), and "what did we believe at TT" (audit) are all
    one query. Scoped per (owner_id, scope) so memory is durable ACROSS
    conversations. Both keys are OPAQUE strings the integrating app supplies
    (D26/D59) — the service stores no user or agent entity and no relationship
    between them; the app decides the sharing topology by choosing which scope
    each run passes (same scope shares, distinct scopes isolate). Cross-`owner_id`
    isolation is the one hard boundary (D54).
    """
    __tablename__ = "ai_memories"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)

    owner_id = Column(String, nullable=False)          # The user the memory is about
    scope = Column(String, nullable=False)              # The memory namespace (D59); agents sharing it share memory

    text = Column(Text, nullable=False)                 # The natural-language fact
    # Normalized md5 of `text` — the exact-restatement dedup fast path (mem0):
    content_hash = Column(String, nullable=False, index=True)
    # Optional normalized "slot" (e.g. "residence") — O(1) lookup of the current
    # holder of a single-valued claim for supersession (cognee deterministic id):
    claim_key = Column(String, nullable=True)
    # Embedding as portable JSON (list[float]); NULL until the background pass
    # fills it — search guards on NOT NULL (same pattern as the transcript):
    embedding = Column(JSONColumn, nullable=True)

    # Valid-time (when the fact was true IN THE WORLD) — nullable when unstated:
    valid_at = Column(DateTime(timezone=True), nullable=True)
    invalid_at = Column(DateTime(timezone=True), nullable=True)
    # Transaction-time (when WE believed it): recorded_at = learned; superseded_at
    # = retired (NULL = this row is the currently-believed truth):
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by = Column(Uuid(as_uuid=True), nullable=True)   # The fact that replaced this one

    # Provenance: transcript item ids that produced/corroborated this fact
    # (append-on-dedup, Graphiti `episodes[]`) — for audit + conflict resolution:
    source_turn_ids = Column(JSONColumn, nullable=False, default=list)
    meta = Column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        # The run-time recall path scans (owner, agent, still-live):
        Index("ix_ai_memories_scope_live", "owner_id", "scope", "superseded_at"),
    )
