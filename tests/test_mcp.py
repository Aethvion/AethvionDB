"""
tests/test_mcp.py
Smoke test for the Layer-2 MCP server (P2-13). Skipped where `mcp` isn't
installed (it's a separate optional package under layer2/).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_MCP_DIR = Path(__file__).resolve().parent.parent / "layer2" / "aethviondb-mcp"


@pytest.fixture(scope="module")
def mcp_server():
    sys.path.insert(0, str(_MCP_DIR))
    try:
        import mcp_server as srv
        return srv
    finally:
        sys.path.remove(str(_MCP_DIR))


def test_server_and_tools_present(mcp_server):
    # FastMCP instance plus the expected tool callables are exported.
    assert mcp_server.mcp.name == "aethviondb"
    for tool in ("search_entities", "get_entity", "upsert_entity", "add_relation",
                 "update_entity", "delete_entity", "list_neighbors", "traverse_graph",
                 "find_path", "database_health", "list_databases"):
        assert callable(getattr(mcp_server, tool)), f"missing tool: {tool}"


def test_client_helper_uses_config(mcp_server, monkeypatch):
    from aethviondb import AethvionClient
    c = mcp_server._client("somedb")
    assert isinstance(c, AethvionClient) and c.db == "somedb"
