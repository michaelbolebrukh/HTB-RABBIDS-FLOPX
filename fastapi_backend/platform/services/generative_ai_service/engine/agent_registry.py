"""
Agent registry — agent specs resolvable by name (Phase 9).

A minimal registry so the HTTP surface can look up an agent's spec (and its
`allow_user_mcp` / `allow_user_skills` toggles) by name when a user manages
their own extensions, and so the run flow has one home for named agents rather
than a hardcoded default. Specs are DATA (D19); registration is code (D33), the
same trust boundary as the tool table — a product registers its agents here.

Registration is idempotent (last write wins) so import-order and re-registration
in tests never raise; agents are just data.
"""

from __future__ import annotations

from .types import AgentSpec


_AGENTS: dict[str, AgentSpec] = {}


def register_agent(spec: AgentSpec) -> None:
    """Registering (or replacing) an agent spec under its name:"""
    _AGENTS[spec.name] = spec


def get_agent(name: str) -> AgentSpec | None:
    """Looking up an agent spec by name (None when absent):"""
    return _AGENTS.get(name)


def registered_agent_names() -> list[str]:
    """Listing the registered agent names (introspection/diagnostics):"""
    return sorted(_AGENTS)
