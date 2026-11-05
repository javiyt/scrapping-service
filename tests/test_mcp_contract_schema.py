"""Contract snapshot tests for the MCP adapter's tool schemas (ticket #150).

The tool list, names, descriptions and JSON input schemas are the adapter's
public contract: MCP hosts render them to the model and clients generate
calls from them.  These tests pin the *shape* of the contract (not byte-for-
byte text) so a rename, removal, or input-schema break is caught in CI the
way a REST contract change would be.

Snapshot strategy: the reference snapshot lives in
``tests/snapshots/mcp_tool_schemas.json``.  The tests assert equality with it;
when a contract change is *intended*, regenerate the snapshot deliberately
with ``make mcp-snapshot`` — never by editing tests.
"""

from __future__ import annotations

import json
import os

import pytest

from app.mcp.client import ScrapperClient
from app.mcp.observability import MCPMetrics
from app.mcp.tools import ToolRegistry

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "snapshots", "mcp_tool_schemas.json")


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    # No network: the registry is built from a client that never fires.
    client = ScrapperClient(base_url="http://contract.invalid")
    return ToolRegistry(client, MCPMetrics())


def _sorted_schemas(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(tools, key=lambda t: t["name"])


def test_tool_schema_snapshot_matches_reference(registry: ToolRegistry) -> None:
    """Every tool's name, description and inputSchema matches the saved contract."""
    with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
        reference = json.load(fh)
    current = _sorted_schemas(registry.list_tools())
    assert current == reference, (
        "The MCP tool contract changed. If the change is intentional, "
        "regenerate the snapshot with: make mcp-snapshot"
    )


def test_tool_names_are_stable_and_unique(registry: ToolRegistry) -> None:
    names = registry.names()
    assert len(names) == len(set(names))
    # The contract includes the documented tool families — regressions in any
    # of them break downstream agents.
    for expected in (
        "adapter_info",
        "search",
        "search_capabilities",
        "fetch_url",
        "fetch_url_batch",
        "get_youtube_transcript",
        "browser_capabilities",
        "browser_create_session",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_evaluate",
        "browser_close_tab",
        "browser_close_session",
        "cache_purge",
        "cache_delete",
        "cache_cleanup",
        "cache_vacuum",
        "cache_stats",
    ):
        assert expected in names, f"documented tool '{expected}' missing from contract"


def test_input_schemas_are_valid_json_schema(registry: ToolRegistry) -> None:
    """Each inputSchema is a well-formed JSON Schema object for an object input."""
    for spec in registry.list_tools():
        schema = spec["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema.get("required"), list)
        # Nothing required may be missing from the property table.
        for required in schema["required"]:
            assert required in schema["properties"], (
                f"{spec['name']}: required '{required}' has no property"
            )
        # Every property must itself be a schema object with a type or enum.
        for prop, sub in schema["properties"].items():
            assert isinstance(sub, dict), f"{spec['name']}.{prop} is not a schema object"
            assert "type" in sub or "enum" in sub, f"{spec['name']}.{prop} has no type or enum"


def test_descriptions_are_non_empty_and_reasonable(registry: ToolRegistry) -> None:
    for spec in registry.list_tools():
        assert spec["description"].strip(), f"{spec['name']} has an empty description"
        assert len(spec["description"]) < 600, f"{spec['name']} description is suspiciously long"


def test_required_arguments_flagged_in_schema(registry: ToolRegistry) -> None:
    """Tools with mandatory inputs mark them required (the model needs to know)."""
    by_name = {t["name"]: t for t in registry.list_tools()}
    assert "url" in by_name["fetch_url"]["inputSchema"]["required"]
    assert "query" in by_name["search"]["inputSchema"]["required"]
    assert "session_id" in by_name["browser_click"]["inputSchema"]["required"]
    assert "tab_id" in by_name["browser_click"]["inputSchema"]["required"]
    # adapter_info takes no arguments
    assert by_name["adapter_info"]["inputSchema"]["required"] == []


def test_snapshot_file_exists_and_is_valid_json() -> None:
    """The reference snapshot must exist even before the first regeneration."""
    assert os.path.exists(SNAPSHOT_PATH), (
        "tests/snapshots/mcp_tool_schemas.json is missing — run: make mcp-snapshot"
    )
    with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list) and data, "snapshot must be a non-empty JSON array"
