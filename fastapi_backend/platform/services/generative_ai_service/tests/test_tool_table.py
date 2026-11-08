"""
Tool-table tests (P5, D33/D34): registration/duplicate/unknown-name behavior,
spec-name resolution, implicit feature-toggle names, and the native-vs-fill
split. Pure — no SDK, no database, no network.
"""

import pytest

from platform.services.generative_ai_service.engine.tool_table import (
    _TOOL_TABLE,
    register_tool,
    registered_tool_names,
    requested_tool_names,
    resolve_tools,
    split_native_tools,
)
from platform.services.generative_ai_service.engine.types import (
    AgentFeatures,
    AgentSpec,
    ToolAlreadyRegisteredError,
    ToolNotRegisteredError,
)
from platform.services.generative_ai_service.registry.schema import Capabilities


@pytest.fixture()
def clean_table():
    """Snapshotting the global table around each test so registrations don't leak:"""
    snapshot = dict(_TOOL_TABLE)
    yield
    _TOOL_TABLE.clear()
    _TOOL_TABLE.update(snapshot)


async def _noop(ctx, x: str) -> str:
    """A minimal tool body for registration tests."""
    return x


def test_register_and_resolve(clean_table):
    register_tool("t_alpha", _noop, description="alpha")
    register_tool("t_beta", _noop, description="beta")
    resolved = resolve_tools(["t_beta", "t_alpha", "t_beta"])
    # Order preserved, duplicates dropped:
    assert [r.name for r in resolved] == ["t_beta", "t_alpha"]
    assert resolved[0].description == "beta"
    assert "t_alpha" in registered_tool_names()


def test_duplicate_registration_raises(clean_table):
    register_tool("t_dup", _noop, description="first")
    with pytest.raises(ToolAlreadyRegisteredError):
        register_tool("t_dup", _noop, description="second")


def test_unknown_name_raises_loudly(clean_table):
    # The error names what IS registered, so a typo is diagnosable from the message:
    with pytest.raises(ToolNotRegisteredError, match="no_such_tool"):
        resolve_tools(["no_such_tool"])


def test_requested_tool_names_implicit_toggles():
    # rag_search (default True) implies search_conversation; web_search off by default:
    spec = AgentSpec(name="a", model="test:m", tools=["custom"])
    assert requested_tool_names(spec) == ["custom", "search_conversation"]

    # Toggles imply their names without duplicating explicit entries:
    spec = AgentSpec(
        name="a", model="test:m", tools=["search_conversation"],
        features=AgentFeatures(web_search=True),
    )
    assert requested_tool_names(spec) == ["search_conversation", "web_search"]

    # Everything off => exactly the explicit list:
    spec = AgentSpec(name="a", model="test:m", features=AgentFeatures(rag_search=False))
    assert requested_tool_names(spec) == []


def test_split_native_tools_registry_decides():
    # The D34 golden table: dual names follow the model's capability flags;
    # ordinary names always resolve as ours:
    names = ["web_search", "web_fetch", "search_conversation", "code_execution"]

    all_native = Capabilities(web_search=True, web_fetch=True, code_execution=True)
    native, fills = split_native_tools(names, all_native)
    assert native == ["web_search", "web_fetch", "code_execution"]
    assert fills == ["search_conversation"]

    none_native = Capabilities()
    native, fills = split_native_tools(names, none_native)
    assert native == []
    assert fills == names

    mixed = Capabilities(web_search=True)
    native, fills = split_native_tools(names, mixed)
    assert native == ["web_search"]
    assert fills == ["web_fetch", "search_conversation", "code_execution"]
