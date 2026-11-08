"""
generate_image fill — image output for every model (Phase 7, D41).

The client-side supplement that supersedes D40's "no fill": when an agent has
`features.image_generation` on but its serving model cannot natively emit
images (per the registry), the engine injects THIS tool. The model calls it
like any function tool; the tool runs the spec's pickable image model (D42)
through the media port and stages the image for persistence.

The image itself never enters the model's context: the staged MediaPart item
is appended with the run's outcome (atomic, D41 collector on ToolContext) and
surfaces to clients as the SAME MediaGenerated event and transcript item the
native path produces — callers cannot tell which ran.

Registered at import (D33); the package __init__ imports this module for the
side effect, exactly like platform_tools and the sandbox code tool.
"""

from __future__ import annotations

import logging

from .engine.tool_table import ToolContext, register_tool
from .engine.types import ToolExecutionError
from .media import MediaError, generate_image as _port_generate_image
from .transcript.types import Item, ItemKind, MediaPart, Role


logger = logging.getLogger(__name__)


async def generate_image(ctx: ToolContext, prompt: str, size: str = "1024x1024") -> dict:
    """
    Generate an image from a text prompt. The image is stored in the
    conversation and shown to the user; this returns a small confirmation.
    """
    try:
        result = await _port_generate_image(prompt, ctx.media, size=size)
    except MediaError as exc:
        # Expected failures (missing lane key, provider error) become a
        # reactable tool result rather than a dead run (P5 translation rule):
        raise ToolExecutionError(str(exc)) from exc

    # Staging the image for atomic persistence with the run's outcome (D41);
    # meta mirrors the native path so analytics/rendering treat both alike:
    ctx.media_items.append(Item(
        conversation_id=ctx.conversation_id, kind=ItemKind.message, role=Role.assistant,
        parts=[MediaPart(
            media="image", mime_type=result.mime_type,
            data_b64=result.data_b64, url=result.url,
        )],
        model_used=result.model_id,
        meta={"generated": True, "implementation": "ours"},
    ))

    # A small reference is all the model needs to narrate what it made:
    return {
        "status": "generated",
        "model": result.model_id,
        "mime_type": result.mime_type,
        "note": "The image is stored in the conversation and visible to the user.",
    }


# Registering at import (D33). The docstring above is what the model reads:
register_tool(
    "generate_image",
    generate_image,
    description=(
        "Generate an image from a text prompt. The image is stored in the conversation "
        "and shown to the user automatically. Use a detailed, concrete prompt."
    ),
)
