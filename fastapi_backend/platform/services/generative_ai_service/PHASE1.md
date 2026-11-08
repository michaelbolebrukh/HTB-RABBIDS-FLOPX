# Phase 1 - Transcript Store: Full Development Spec

Status: approved for development (July 2026). Companion to `README.md`
(goals, D1–D27). This file is the buildable spec; snippets are normative for
interfaces, illustrative for bodies.

## Goals / non-goals

**Goals**: provider-independent, append-only conversation record with branching,
summary cache, token bookkeeping, and search - the source of truth every later
phase renders from.

**Non-goals (explicitly out of P1)**: model calls, Pydantic AI imports,
summarization policy (P2 renderer), registry (P3), budgets (P4), UI events.

## Extraction-readiness rules (D26)

This package will eventually leave QUANTSOC and become its own web service that
multiple consumers plug into. Therefore:

- **Own module files**: `models.py`, `schemas` (in `transcript/types.py`),
  `exceptions.py` live inside this package - never in the host backend's
  `platform/models.py`.
- **Table namespace**: every table is prefixed `ai_` (`ai_conversations`,
  `ai_transcript_items`, `ai_transcript_summaries`).
- **No FK into host tables**: conversations carry `owner_id: str` (indexed, no
  foreign key to `users`). The consumer maps its own identity onto it. QUANTSOC
  passes `str(user.id)`.
- **Documented host couplings only** (each must be trivially replaceable at
  extraction): `infrastructure/db_types` (portable column types - copy into the
  package on extraction), the host's session/migration machinery, and the host
  settings object (injected, not imported deep in logic).
- Python package: `platform/services/generative_ai_service/` for now; the
  internal layout below is the layout the future standalone service keeps.

## Package layout

```
generative_ai_service/
  README.md            # goals + decision record (D1-D27)
  PHASE1.md            # this spec
  models.py            # ai_-prefixed SQLAlchemy tables (package-owned)
  exceptions.py        # package exceptions
  transcript/
    __init__.py        # public exports
    types.py           # Item, ContentPart union, enums, Usage (pure Pydantic)
    store.py           # service functions (session-first, per repo rules)
    search.py          # text search now; embedding scan behind same API
  tests/
    test_types.py      # round-trip serialize/deserialize every kind
    test_store.py      # append/branch/path/full-history against SQLite
    test_search.py     # search API contract
```

Legacy modules (`agents/`, `memory/`, `llms.py`, `types.py`, `utils/`) remain
untouched during P1 and are deleted in P2 when the engine replaces their callers.

## Types (`transcript/types.py`)

```python
class ItemKind(str, Enum):
    message = "message"            # user/assistant/system text+media turn
    tool_call = "tool_call"        # model requested a tool
    tool_result = "tool_result"    # tool returned
    thinking = "thinking"          # reasoning summary/content (if surfaced)
    summary_marker = "summary_marker"  # rendering hint; NOT a summary itself
    event = "event"                # anything else: voice session, sandbox, UI


class Role(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class MediaPart(BaseModel):
    type: Literal["media"] = "media"
    media: Literal["image", "audio", "video", "file"]
    url: str | None = None
    data_b64: str | None = None
    mime_type: str | None = None
    transcript: str | None = None      # audio/video transcript when known
    width: int | None = None
    height: int | None = None
    filename: str | None = None


class DataPart(BaseModel):
    type: Literal["data"] = "data"
    data: dict                          # tool args/results, structured output


ContentPart = Annotated[TextPart | MediaPart | DataPart, Field(discriminator="type")]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


class Item(BaseModel):
    id: UUID | None = None              # set by store on append
    conversation_id: UUID
    parent_id: UUID | None = None       # branching (D24, legacy tree carried)
    kind: ItemKind
    role: Role | None = None
    parts: list[ContentPart] = []
    tool_name: str | None = None        # kind in {tool_call, tool_result}
    tool_call_id: str | None = None
    model_used: str | None = None       # which model produced this item
    usage: Usage | None = None          # provider-reported, attached in P2
    token_estimates: dict[str, int] = {}  # per tokenizer-family cache (legacy TokenCache idea)
    raw: dict | None = None             # opaque framework/provider payload (D24)
    meta: dict = {}
    created_at: datetime | None = None
```

Notes:
- **Two-lane rendering invariant (comment this thoroughly in code):** our fields
  (`kind`/`role`/`parts`/...) are the universal representation - they serve BOTH
  our own layer (UI, summarizer, RAG, cost meter) AND translation into Pydantic
  AI whenever the fast lane doesn't apply. `raw` is the fast lane only: consulted
  when present AND the next run targets the same provider it came from (lossless
  replay of thinking/reasoning/tool internals). Many items never have `raw`
  (user-authored, injected summaries, `event` items, imports). An item MUST be
  fully renderable to any provider from our fields alone; `raw` is a bonus,
  never a requirement.
- `raw` holds Pydantic AI's serialized message for lossless same-provider
  replay; readers never parse it; dropped when rendering cross-provider.
- `event` items make voice sessions and sandbox runs storable in P6 without a
  schema change - payload goes in `DataPart`/`meta`.
- `summary_marker` items are optional bookmarks; actual summaries live in the
  cache table (summaries are cache entries, never transcript content - D4).

## Tables (`models.py` + manual migration, repo DB rules apply)

```python
class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id = Column(String, nullable=False, index=True)   # host-agnostic (D26)
    title = Column(String, nullable=True)
    current_item_id = Column(Uuid(as_uuid=True), nullable=True)  # active leaf
    meta = Column(JSONColumn, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AITranscriptItem(Base):
    __tablename__ = "ai_transcript_items"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id = Column(Uuid(as_uuid=True), ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(Uuid(as_uuid=True), ForeignKey("ai_transcript_items.id"), nullable=True, index=True)
    kind = Column(String, nullable=False, index=True)        # ItemKind.value
    role = Column(String, nullable=True)
    parts = Column(JSONColumn, nullable=False, default=list)
    tool_name = Column(String, nullable=True)
    tool_call_id = Column(String, nullable=True)
    model_used = Column(String, nullable=True)
    usage = Column(JSONColumn, nullable=True)
    token_estimates = Column(JSONColumn, nullable=False, default=dict)
    raw = Column(JSONColumn, nullable=True)
    meta = Column(JSONColumn, nullable=False, default=dict)
    text_content = Column(Text, nullable=True)               # extracted text for search
    embedding = Column(JSONColumn, nullable=True)            # list[float], portable
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class AITranscriptSummary(Base):
    __tablename__ = "ai_transcript_summaries"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id = Column(Uuid(as_uuid=True), ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    span_end_item_id = Column(Uuid(as_uuid=True), nullable=False)  # summary covers root..span_end (legacy boundary-node idea)
    token_budget = Column(Integer, nullable=False)
    tokenizer_family = Column(String, nullable=False)
    summary_text = Column(Text, nullable=False)
    usage = Column(JSONColumn, nullable=True)                # cost of producing it
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("conversation_id", "span_end_item_id", "token_budget", "tokenizer_family"),
        Index("ix_ai_summaries_lookup", "conversation_id", "span_end_item_id"),
    )
```

Migration: one manual Alembic migration, upgrade + downgrade, portable ops only.

## Store API (`transcript/store.py`) - session-first per repo rules

```python
def create_conversation(db: Session, owner_id: str, title: str | None = None) -> AIConversation: ...

def append_item(db: Session, item: Item) -> Item:
    """Appends to the conversation's current leaf (or item.parent_id when given);
    updates conversation.current_item_id; extracts text_content; never mutates
    existing rows."""

def branch_from(db: Session, parent_id: UUID, item: Item) -> Item:
    """Creates a sibling branch (message editing); moves current_item_id."""

def get_path(db: Session, item_id: UUID) -> list[Item]:
    """Root -> item via recursive CTE (carried from legacy loader)."""

def full_history(db: Session, conversation_id: UUID) -> list[Item]:
    """Every item, all branches, chronological."""

def get_summary(db: Session, conversation_id: UUID, span_end_item_id: UUID,
                token_budget: int, tokenizer_family: str) -> AITranscriptSummary | None: ...

def put_summary(db: Session, ...) -> AITranscriptSummary: ...
```

Async callers wrap with `asyncio.to_thread` until O2 (async sessions) is
resolved - same pattern the legacy `Memory` used.

## Search (`transcript/search.py`)

One public function, two backends behind it:

```python
def search_conversation(db: Session, conversation_id: UUID, query: str, k: int = 8) -> list[Item]: ...
```

- **v1 (ships with P1, keyless)**: text match over `text_content`
  (ILIKE / FTS where available).
- **v2 (flag-gated, needs an embeddings key)**: background task embeds
  text-bearing items into `embedding`; search embeds the query and ranks by
  cosine **in Python** over the conversation's vectors. A single conversation is
  thousands of items at most - no pgvector needed, stays dialect-portable per
  repo DB rules. Revisit pgvector only if cross-conversation search at scale
  becomes a requirement.

The API is the contract; P5 exposes it to agents as the `search_conversation`
tool unchanged.

## Tests

- `test_types.py`: every `ItemKind` × every part type round-trips through JSON
  exactly (model_dump / model_validate).
- `test_store.py`: append chain, branch-and-edit (sibling), `get_path` returns
  the active branch only, `full_history` returns all branches, current-leaf
  tracking, summary-cache unique-key behavior. Runs on SQLite.
- `test_search.py`: text search finds/misses correctly; embedding backend
  behind a fake embedder.

No network, no keys, no AI SDKs anywhere in P1 tests.

## Done criteria

A conversation can be created, appended, branched, reloaded, searched, and
reconstructed exactly; `agent:check`, tests, and migration upgrade/downgrade all
pass; nothing imports `pydantic_ai` or any provider SDK.

## Provider API keys (D27 - needed at P2, not P1)

Strategy: **direct keys for the big four + one OpenRouter key for the long
tail. No gateway product.**

| Provider | Key env var | Where to create | Notes |
|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` | platform.openai.com | set a monthly budget cap in dashboard |
| Anthropic | `ANTHROPIC_API_KEY` | console.anthropic.com | same |
| Google (Gemini) | `GOOGLE_API_KEY` | aistudio.google.com | AI Studio key (not Vertex) to start |
| xAI (Grok) | `XAI_API_KEY` | console.x.ai | same |
| OpenRouter | `OPENROUTER_API_KEY` | openrouter.ai | one key = hundreds of long-tail models |

Local: `fastapi_backend/.env` (gitignored). Production: Render env / secret
files. Cloud agents: Runtime Secrets. Keys are read by our settings and passed
explicitly at model construction (D17) - no ambient env reliance in engine code.
