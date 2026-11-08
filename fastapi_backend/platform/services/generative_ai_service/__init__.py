"""
Generative AI Service — provider-independent AI baseline (rebuild in progress).

Phase 1 (transcript store) is the live surface, exported below. See README.md
for the goal/decision record (D1-D28) and PHASE1.md for the buildable spec.

The legacy modules still on disk (`agents/`, `memory/`, `llms.py`, `types.py`,
`utils/`) are REFERENCE-ONLY (superseded architecture, README "Legacy code
review") and are intentionally no longer imported here — some depend on
packages the backend no longer installs. They are deleted in Phase 2 when the
engine replaces their last conceptual role.
"""

from .exceptions import (
    ConversationNotFoundError,
    InvalidItemError,
    ItemNotFoundError,
    TranscriptError,
)
from .models import AIConversation, AIMemory, AITranscriptItem, AITranscriptSummary
from .purge import purge_owner
from .transcript import (
    ContentPart,
    DataPart,
    EmbedderProtocol,
    Item,
    ItemKind,
    MediaPart,
    Role,
    TextPart,
    Usage,
    append_item,
    branch_from,
    content_parts_adapter,
    create_conversation,
    delete_conversation,
    embed_pending_items,
    full_history,
    get_conversation,
    get_current_path,
    get_path,
    get_summary,
    put_summary,
    search_conversation,
    set_current,
)

# Registering the built-in platform tools into the tool table at import time
# (D33: registration is code; the module's register_tool calls run on import).
# Product tools register the same way from the product boundary:
from . import platform_tools  # noqa: F401,E402 — imported for its registration side effect

# Registering the code_execution fill the same way (P6, D38): the tool exists
# on every deployment; whether it can actually RUN is the backend's concern:
from .sandbox import code_tool  # noqa: F401,E402 — imported for its registration side effect

# Registering the generate_image fill (P7, D41): image output for every model —
# the engine injects it when the serving model lacks the native capability:
from . import image_tool  # noqa: F401,E402 — imported for its registration side effect

# Registering the open_skill disclosure tool (P9, D53): attached skills expose
# their SKILL.md bodies on demand, scripts running via code_execution:
from .extensions import skill_catalog  # noqa: F401,E402 — imported for its registration side effect

# Registering the durable-memory tools (P10): memory_write/search/supersede/delete
# — the engine exposes them per-agent behind features.memory:
from . import memory as _memory  # noqa: F401,E402 — imported for its registration side effect


__all__ = [
    # Exceptions:
    "TranscriptError", "ConversationNotFoundError", "ItemNotFoundError", "InvalidItemError",
    # ORM models:
    "AIConversation", "AIMemory", "AITranscriptItem", "AITranscriptSummary",
    # Item types:
    "ContentPart", "DataPart", "Item", "ItemKind", "MediaPart", "Role", "TextPart", "Usage",
    "content_parts_adapter",
    # Store:
    "append_item", "branch_from", "create_conversation", "delete_conversation",
    "full_history", "get_conversation", "get_current_path", "get_path",
    "get_summary", "put_summary", "set_current",
    # Search:
    "EmbedderProtocol", "embed_pending_items", "search_conversation",
]
