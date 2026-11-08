"""
Transcript item types — the provider-independent source of truth (Phase 1).

THE TWO-LANE RENDERING INVARIANT (D24, PHASE1.md):

Every field on `Item` except `raw` forms OUR universal representation. These
fields serve BOTH directions of the system:

- The read path: the chat UI, the summarizer, the RAG indexer, cost analytics —
  everything we build reads these fields and NEVER parses `raw`.
- The translation path: when a run targets a provider different from the one an
  item came from (or the item never came from a provider at all), the P2
  renderer builds the outgoing SDK messages FROM these fields.

`raw` is the fast lane ONLY: it holds the AI framework's serialized message
exactly as produced, and is consulted solely when (a) it is present AND (b) the
next run targets the same provider the item came from. That replay is lossless
for provider-specific internals (thinking signatures, reasoning items,
server-side tool-call ids) that our universal fields intentionally do not model.

The invariant: an Item MUST be fully renderable to ANY provider from our fields
alone. `raw` is a bonus, never a requirement. Many items never have `raw` at
all (user-authored messages, injected summaries, `event` items from voice or
sandbox sessions, imported conversations). If an item ever exists whose meaning
lives only in `raw`, the design has been violated.

No `pydantic_ai` or provider SDK imports are allowed anywhere in this module.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, model_validator


# ============================================================================
# ENUMS
# ============================================================================

class ItemKind(str, Enum):
    """
    Discriminates what an item IS. One flat item stream per conversation branch
    covers every shape of content — this is what lets voice sessions and sandbox
    runs (P6) be stored later without schema changes.
    """
    message = "message"                # A user/assistant/system turn (text and/or media)
    tool_call = "tool_call"            # The model requested a tool invocation
    tool_result = "tool_result"        # A tool returned a result
    thinking = "thinking"              # Surfaced reasoning content/summary from the model
    summary_marker = "summary_marker"  # Rendering bookmark; the summary text itself lives in the cache table
    event = "event"                    # Anything else: voice-session frames, sandbox events, UI actions


class Role(str, Enum):
    """Who authored a message-shaped item. Stored as the `.value` string (repo enum rule)."""
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


# ============================================================================
# CONTENT PARTS (discriminated union on `type`)
# ============================================================================

class TextPart(BaseModel):
    """Plain text content — the overwhelmingly common case."""
    type: Literal["text"] = "text"
    text: str


class MediaPart(BaseModel):
    """
    Any non-text media, inbound or outbound. Exactly one of `url` / `data_b64`
    should normally be set; both are allowed (e.g. a cached copy of a URL).
    """
    type: Literal["media"] = "media"
    media: Literal["image", "audio", "video", "file"]
    url: str | None = None
    data_b64: str | None = None
    mime_type: str | None = None
    # Transcript of audio/video when known — also feeds text search extraction:
    transcript: str | None = None
    width: int | None = None
    height: int | None = None
    filename: str | None = None


class DataPart(BaseModel):
    """
    Structured payloads: tool-call arguments, tool results, structured output
    objects, event payloads. Kept as a plain dict so the transcript never
    depends on any particular schema.
    """
    type: Literal["data"] = "data"
    data: dict


# Discriminated union — pydantic dispatches on the `type` literal:
ContentPart = Annotated[Union[TextPart, MediaPart, DataPart], Field(discriminator="type")]

# Reusable adapter for (de)serializing lists of parts to/from JSON columns:
content_parts_adapter: TypeAdapter[list[ContentPart]] = TypeAdapter(list[ContentPart])


# ============================================================================
# USAGE
# ============================================================================

class Usage(BaseModel):
    """
    Provider-reported token usage for a model-produced item. Attached by the P2
    engine after each run; the P4 cost meter prices these against the registry.
    Cache fields matter: cached-token billing differs by an order of magnitude.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # Audio token dimensions (P6 voice) — None for text-only runs; priced at
    # the registry's input_audio/output_audio rates when present:
    input_audio_tokens: int | None = None
    output_audio_tokens: int | None = None


# ============================================================================
# ITEM
# ============================================================================

class Item(BaseModel):
    """
    One entry in a conversation transcript. Append-only: items are never
    mutated after creation — "editing" a message means creating a sibling
    branch (same `parent_id`), which is the mechanism behind user-editable
    messages.

    Field roles at a glance (see module docstring for the full invariant):
    - Identity/structure: `id`, `conversation_id`, `parent_id`, `created_at`.
    - Universal content (read path + translation path): `kind`, `role`,
      `parts`, `tool_name`, `tool_call_id`.
    - Bookkeeping: `model_used` (which model PRODUCED this item — None for
      user-authored items by definition, not by accident; drives cost
      attribution, same-provider replay decisions, and eval slicing),
      `usage` (provider-reported tokens), `token_estimates` (our cached
      counts per tokenizer family, feeding the P2 renderer's window math).
    - Fast lane: `raw` (framework-serialized message; same-provider replay only).
    - Extension: `meta` (free-form; never load-bearing for rendering).
    """
    id: UUID | None = None                    # Assigned by the store on append
    conversation_id: UUID
    parent_id: UUID | None = None             # None = a root item (a conversation may have several roots via branching)
    kind: ItemKind
    role: Role | None = None
    parts: list[ContentPart] = Field(default_factory=list)

    # Tool linkage — only meaningful for tool_call / tool_result kinds:
    tool_name: str | None = None
    tool_call_id: str | None = None

    # Which model produced this item (None for anything not model-generated):
    model_used: str | None = None
    usage: Usage | None = None

    # Per-tokenizer-family token-count cache (carried from the legacy TokenCache idea):
    token_estimates: dict[str, int] = Field(default_factory=dict)

    # Fast-lane payload — see module docstring; readers must never parse this:
    raw: dict | None = None

    meta: dict = Field(default_factory=dict)
    created_at: datetime | None = None        # Assigned by the database on append

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "Item":
        """
        Enforcing kind/field consistency so DB nullability never becomes
        ambiguity in code (the single-table pattern's contract):
        """
        # Tool items must carry a tool name; non-tool items must not:
        if self.kind in (ItemKind.tool_call, ItemKind.tool_result):
            if not self.tool_name:
                raise ValueError(f"{self.kind.value} items require tool_name")
        elif self.tool_name or self.tool_call_id:
            raise ValueError(f"{self.kind.value} items must not carry tool_name/tool_call_id")

        # Message items must have an author:
        if self.kind == ItemKind.message and self.role is None:
            raise ValueError("message items require a role")

        return self

    def extract_text(self) -> str | None:
        """
        Concatenating the searchable text of this item (TextParts plus media
        transcripts). Feeds the `text_content` column that powers keyless
        search — intentionally lossy and one-way; never used for rendering.
        """
        pieces: list[str] = []
        for part in self.parts:
            if isinstance(part, TextPart):
                pieces.append(part.text)
            elif isinstance(part, MediaPart) and part.transcript:
                pieces.append(part.transcript)
        return "\n".join(pieces) if pieces else None
