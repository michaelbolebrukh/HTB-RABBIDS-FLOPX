"""
Transcript package — Phase 1 public surface.

Everything later phases touch is exported here; internals (`_row_to_item`
etc.) stay private. No AI SDK imports anywhere in this package (P1 contract).
"""

from .search import EmbedderProtocol, SearchScope, embed_pending_items, search_conversation
from .store import (
    append_item,
    append_items,
    branch_from,
    create_conversation,
    delete_conversation,
    full_history,
    get_conversation,
    get_current_path,
    get_path,
    get_summary,
    put_summary,
    set_current,
    set_items_meta,
)
from .types import (
    ContentPart,
    DataPart,
    Item,
    ItemKind,
    MediaPart,
    Role,
    TextPart,
    Usage,
    content_parts_adapter,
)


__all__ = [
    # Types:
    "ContentPart", "DataPart", "Item", "ItemKind", "MediaPart", "Role", "TextPart", "Usage",
    "content_parts_adapter",
    # Store:
    "append_item", "append_items", "branch_from", "create_conversation", "delete_conversation",
    "full_history", "get_conversation", "get_current_path", "get_path",
    "get_summary", "put_summary", "set_current", "set_items_meta",
    # Search:
    "EmbedderProtocol", "SearchScope", "embed_pending_items", "search_conversation",
]
