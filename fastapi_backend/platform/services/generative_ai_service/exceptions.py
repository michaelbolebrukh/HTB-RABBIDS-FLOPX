"""
Package exceptions — deliberately framework-free (D26 extraction rules).

Unlike the host backend's `platform/exceptions.py` (which subclasses FastAPI's
HTTPException directly), this package raises plain exceptions so it carries no
web-framework dependency into its future life as a standalone service. Host
routers translate these to HTTP responses at the API boundary.
"""


class TranscriptError(Exception):
    """Base class for every transcript-store error (catch-all for callers)."""


class ConversationNotFoundError(TranscriptError):
    """Raised when a conversation id does not exist (or was deleted)."""

    def __init__(self, conversation_id):
        super().__init__(f"Conversation not found: {conversation_id}")
        self.conversation_id = conversation_id


class ItemNotFoundError(TranscriptError):
    """Raised when an item id does not exist in the expected conversation."""

    def __init__(self, item_id):
        super().__init__(f"Transcript item not found: {item_id}")
        self.item_id = item_id


class InvalidItemError(TranscriptError):
    """Raised when an item fails structural rules beyond pydantic validation
    (e.g. parent belongs to a different conversation)."""
