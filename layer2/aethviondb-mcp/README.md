# AethvionDB MCP Server

Exposes AethvionDB as native [Model Context Protocol](https://modelcontextprotocol.io)
tools, so agents (Claude Desktop, Cursor, …) can read and write the shared brain.

It's a thin **Layer 2** translator: every tool calls the Layer-1 HTTP API through
the packaged `AethvionClient`. Run the AethvionDB server first (`aethviondb-server`).

## Tools

| Tool | What it does |
|---|---|
| `search_entities` | keyword search → ranked entities |
| `get_entity` | fetch by ID or exact name |
| `upsert_entity` | create/update by name (deduped) |
| `add_relation` | add a typed edge to a target (stub-created if new) |
| `update_entity` | update an entity's summary |
| `delete_entity` | soft-delete |
| `list_neighbors` | inbound + outbound relations |
| `traverse_graph` | nodes + edges from a start entity |
| `find_path` | shortest relation path between two entities |
| `database_health` | consistency report |
| `list_databases` | available databases |

## Install

```bash
pip install -e .            # installs mcp + aethviondb
```

## Configure

Environment variables (all optional except where a key is required):

| Var | Default | |
|---|---|---|
| `AETHVIONDB_URL` | `http://127.0.0.1:7475` | server base URL |
| `AETHVIONDB_DB` | `default` | database to operate on |
| `AETHVIONDB_API_KEY` | — | required only if the db has keys |
| `AETHVIONDB_ACTOR` | `mcp` | attribution recorded on writes (shows in the live feed) |

### Claude Desktop / Cursor

Add to the MCP servers config:

```jsonc
{
  "mcpServers": {
    "aethviondb": {
      "command": "aethviondb-mcp",
      "env": {
        "AETHVIONDB_URL": "http://127.0.0.1:7475",
        "AETHVIONDB_DB": "shared",
        "AETHVIONDB_ACTOR": "claude"
      }
    }
  }
}
```

Writes are attributed to `AETHVIONDB_ACTOR`, so you can watch the agent work live
in the dashboard's activity feed.
