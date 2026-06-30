"""
AethvionDB MCP server (Layer 2).

Exposes AethvionDB as native MCP tools so agents (Claude Desktop, Cursor, …) can
read and write the shared brain. It is a thin translator: every tool calls the
Layer-1 HTTP API through the packaged, dependency-free ``AethvionClient`` — no
Layer-1 code is imported directly.

Configure via environment:
    AETHVIONDB_URL      base URL of the server   (default http://127.0.0.1:7475)
    AETHVIONDB_DB       default database          (default "default")
    AETHVIONDB_API_KEY  API key, if the db requires one
    AETHVIONDB_ACTOR    attribution for writes    (default "mcp")

Run:  aethviondb-mcp        (or: python mcp_server.py)
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from aethviondb import AethvionClient, AethvionError

mcp = FastMCP("aethviondb")

_BASE  = os.getenv("AETHVIONDB_URL", "http://127.0.0.1:7475")
_DB    = os.getenv("AETHVIONDB_DB", "default")
_KEY   = os.getenv("AETHVIONDB_API_KEY") or None
_ACTOR = os.getenv("AETHVIONDB_ACTOR", "mcp")


def _client(db: str = "") -> AethvionClient:
    return AethvionClient(base_url=_BASE, db=db or _DB, api_key=_KEY, actor=_ACTOR)


def _safe(fn):
    """Turn client errors into a structured tool result instead of raising."""
    try:
        return fn()
    except AethvionError as e:
        return {"error": str(e), "code": e.code, "status": e.status}


@mcp.tool()
def search_entities(query: str, db: str = "", entity_type: str = "", limit: int = 20) -> dict:
    """Search the knowledge base by keyword. Returns ranked matching entities.

    Args:
        query: Text to search for (name, summary, tags).
        db: Database name (defaults to the configured one).
        entity_type: Optional filter, e.g. 'person', 'module', 'service'.
        limit: Max results.
    """
    filters = {"type": entity_type} if entity_type else None
    return _safe(lambda: {"results": _client(db).search(query, filters=filters, limit=limit)})


@mcp.tool()
def get_entity(id_or_name: str, db: str = "") -> dict:
    """Fetch one entity by ID (ws_…) or by exact name."""
    def run():
        c = _client(db)
        if id_or_name.startswith("ws_"):
            e = c.get(id_or_name)
        else:
            hits = c.search(id_or_name, limit=1)
            e = c.get(hits[0]["id"]) if hits else None
        return e or {"error": f"Entity {id_or_name!r} not found"}
    return _safe(run)


@mcp.tool()
def upsert_entity(name: str, type: str = "other", summary: str = "",
                  kind: str = "", db: str = "") -> dict:
    """Create or update an entity by name (deduped by the name index).

    Args:
        name: Canonical name.
        type: Coarse type (person, module, concept, service, …).
        summary: 1–3 sentence description.
        kind: Optional fine-grained sub-type (e.g. 'software.module').
        db: Database name.
    """
    fields = {"type": type}
    if summary:
        fields["summary"] = summary
    if kind:
        fields["kind"] = kind
    return _safe(lambda: _client(db).upsert(name, **fields))


@mcp.tool()
def add_relation(source_id: str, kind: str, target_name: str, note: str = "", db: str = "") -> dict:
    """Add a typed relation from an entity to a target (created as a stub if new).

    Args:
        source_id: ID of the entity the relation starts from.
        kind: Relation kind (depends_on, calls, part_of, related_to, …).
        target_name: Name of the target entity.
        note: Optional note on the edge.
    """
    def run():
        c = _client(db)
        existing = c.get(source_id)
        if not existing:
            return {"error": f"Source entity {source_id!r} not found"}
        rels = existing.get("sections", {}).get("relations", [])
        rels.append({"kind": kind, "target_name": target_name, "note": note})
        # upsert by name carries relations (resolves target_name -> id / stub)
        return c.upsert(existing["name"], relations=rels)
    return _safe(run)


@mcp.tool()
def update_entity(entity_id: str, summary: str = "", db: str = "") -> dict:
    """Update an entity's summary (more fields can be added as tools grow)."""
    mutations = {"sections": {"core": {"summary": summary}}} if summary else {}
    return _safe(lambda: _client(db).update(entity_id, mutations))


@mcp.tool()
def delete_entity(entity_id: str, db: str = "") -> dict:
    """Soft-delete an entity (sets status to deleted)."""
    return _safe(lambda: _client(db).delete(entity_id))


@mcp.tool()
def list_neighbors(entity_id: str, db: str = "") -> dict:
    """List an entity's inbound and outbound relations."""
    return _safe(lambda: _client(db).neighbors(entity_id))


@mcp.tool()
def traverse_graph(start_id: str, depth: int = 2, direction: str = "both", db: str = "") -> dict:
    """Traverse the knowledge graph from a starting entity. Returns nodes + edges."""
    return _safe(lambda: _client(db).traverse(start_id, depth=depth, direction=direction))


@mcp.tool()
def find_path(start_id: str, end_id: str, db: str = "") -> dict:
    """Find the shortest relation path between two entities."""
    return _safe(lambda: _client(db).path(start_id, end_id))


@mcp.tool()
def database_health(db: str = "") -> dict:
    """Run consistency checks and return a health report for the database."""
    return _safe(lambda: _client(db).validate())


@mcp.tool()
def list_databases() -> dict:
    """List the available databases on the server."""
    import urllib.request, json
    try:
        with urllib.request.urlopen(f"{_BASE}/api/v1/") as r:
            data = json.loads(r.read().decode())["data"]
        return {"databases": data.get("databases", [])}
    except Exception as e:
        return {"error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
