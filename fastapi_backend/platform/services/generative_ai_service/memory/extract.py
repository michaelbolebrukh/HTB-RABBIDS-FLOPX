"""
Durable-fact extraction — the second phase of write-completeness (P10/D60).

Real-time `memory_write` (phase one) depends on the agent *choosing* to save a
fact; adversarial testing showed it misses ~30% of terse asides. Phase two is a
safety net: when a span of history is about to be COMPACTED out of the model's
context (summarized), we extract the durable facts from that span before they
are lost and reconcile them into memory. Every extracted fact flows through the
same `reconcile_write`, so anything the agent already saved dedups (no doubles).

Pure strings + a defensive parser here (no `pydantic_ai`, D3); the engine builds
the LLM extractor through the adapter and calls this scaffolding.
"""

from __future__ import annotations

import json
import re


EXTRACT_INSTRUCTIONS = (
    "You extract DURABLE facts about the user from a conversation excerpt that is "
    "about to be archived. Return STRICT JSON, no prose: a list of short, "
    "self-contained fact strings.\n\n"
    "- Include only STABLE, worth-remembering facts: identities, names, "
    "relationships, preferences, ongoing situations, commitments, settings.\n"
    "- Each fact must stand alone out of context (write 'The user's cat is named "
    "Sushi', never 'her cat is Sushi').\n"
    "- EXCLUDE ephemera: pleasantries, one-off task chatter, questions, anything "
    "already obvious or transient.\n"
    "- If nothing durable is present, return [].\n\n"
    'Example: ["The user is allergic to penicillin", "The user prefers metric units"]'
)


def build_extract_prompt(excerpt: str) -> str:
    """Rendering the extractor's user turn from the span's rendered text:"""
    return f"Conversation excerpt to archive:\n\n{excerpt}\n\nDurable facts (JSON list):"


def parse_extract_response(raw: str | list, max_facts: int = 20) -> list[str]:
    """
    Parsing the extractor's JSON into a clean fact list, defensively: a non-list
    or unparseable output yields [] (the flush then no-ops), blanks are dropped,
    over-long strings trimmed, and the count is bounded so one pass cannot flood
    memory. The model's output is untrusted data (P1), handled as such:
    """
    if isinstance(raw, list):
        data = raw
    else:
        try:
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            data = json.loads(match.group(0)) if match else []
        except (json.JSONDecodeError, ValueError, TypeError):
            data = []
    facts: list[str] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            facts.append(text[:500])
    return facts[:max_facts]
