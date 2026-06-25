# AethvionDB

> **A knowledge database engine built for agents.**
> Every fact — person, place, module, decision, task — is a typed entity in one
> uniform envelope, wired into a graph, deduplicated by name, and queryable in
> milliseconds. **Runs entirely on your machine.**

> ⚠️ **Early development (v0.1).** The engine was battle-tested inside
> [Aethvion Suite](https://github.com/Aethvion/Aethvion-Suite) and is now
> extracted here as a standalone package. The API and storage format may still
> change before a stable release.

---

## Quick start

```bash
# From a checkout (editable install)
pip install -e ".[dev]"

# Run the HTTP API (defaults to 127.0.0.1:7475)
aethviondb-server
#   docs at  http://127.0.0.1:7475/docs
#   API at   http://127.0.0.1:7475/api/v1
```

Data is stored under `~/.aethvion/aethviondb` by default (override with the
`AETHVIONDB_DATA_DIR` environment variable). Use it as a library, too:

```python
from aethviondb import EntityWriter
w = EntityWriter()                       # default database
entity, created = w.create("Ada Lovelace", entity_type="person")
print(w.get_by_name("Ada Lovelace")["id"])
```

The optional **intelligence** features (distill text → entity, expand, generate
embeddings) require an injected LLM/embedding backend — the deterministic core
(entities, graph, search, validation) works without one.

---

## What it is

AethvionDB is a local, file-backed knowledge store designed from the start for
**agentic use** — for AI agents and local models to read and write structured
knowledge against a single shared source of truth.

Unlike a wiki (documents) or a vector store (opaque chunks), AethvionDB keeps
**typed entities with explicit relationships**. Nothing is duplicated: facts are
stored once and referenced by ID, so the knowledge stays a graph rather than a
pile of copies.

Where [Aethvion Project Mapper](https://github.com/Aethvion/Aethvion-ProjectMapper)
maps a single codebase, AethvionDB is the general-purpose knowledge layer: the
**brain of the workspace** that many agents and systems can share.

---

## Core model

Every entity shares one envelope:

```jsonc
{
  "schema_version": 1,       // on-disk format version (migrated forward across releases)
  "id": "ws_<hex>",          // stable, content-independent ID
  "type": "person|place|module|service|decision|goal|...",
  "kind": "software.module", // optional fine-grained sub-type
  "name": "Canonical Name",  // aliases live in core.aliases; deduped by name
  "status": "active|stub|deleted|planned|deprecated|experimental",
  "version": 1,              // mutation counter — incremented on every write
  "created": "ISO-8601",
  "updated": "ISO-8601",
  "source": "manual|import|expansion|distilled",
  "sections": {
    "core":       { "summary": "", "aliases": [], "categories": [], "tags": [] },
    "timeline":   [ { "date": "...", "event": "...", "ref_ids": ["ws_..."] } ],
    "relations":  [ { "kind": "depends_on", "target_id": "ws_...", "note": "" } ],
    "properties": { /* type-specific structured facts */ },
    "stubs":      [ "Sub-topic that deserves its own entity" ]
  }
}
```

A global **name index** is consulted before any entity is created, so the same
real-world thing never gets two records. Relationships are first-class and
typed (`depends_on`, `parent_of`, `calls`, `created_by`, `related_to`, …),
making the store a true knowledge graph.

The on-disk format is versioned (`schema_version`) and migrated forward across
releases — see [docs/STORAGE_FORMAT.md](docs/STORAGE_FORMAT.md) for the full
layout and stability policy.

---

## Features

- **Typed entities + typed relations** — one schema for everything, graph-native.
- **Name-index deduplication** — atomic get-or-create; no duplicate records.
- **Distillation** — extract a structured entity from raw text with an LLM.
- **Expansion** — grow the graph from stubs into fully-formed entities.
- **Hybrid + vector search** — keyword and embedding similarity over entities.
- **Graph queries** — traverse, neighbors, and shortest-path between entities.
- **Validation** — schema + cross-entity consistency (duplicates, orphan stubs,
  broken relations, timeline ordering).
- **Baking** — export curated, flattened snapshots for downstream consumers.
- **Backups** — point-in-time copies with restore.
- **Multiple databases** — each an independent directory; switch freely.
- **HTTP API** — a versioned REST surface (`/api/v1`) with API keys, batch
  operations, cursor pagination, and section projection.

---

## Performance

AethvionDB serves reads from an in-memory cache backed by a single-file
snapshot, with an O(1) generation counter for freshness (no per-file `stat()`
scans). Measured on a 30,352-entity / 36 MB database:

| Operation | Time |
|---|---|
| Warm entity list | ~4 ms |
| Cold load (snapshot) | ~350 ms (off the event loop) |
| Freshness check | ~0.1 ms |
| Single write | O(1) — patches the cache, no full rebuild |

The list view is served a lightweight projection; full entity bodies load on
demand. Writes patch the cache in place and bump the generation, so a single
write never triggers a full rebuild.

---

## Architecture

```
aethviondb/
├── entity_schema.py     — the entity envelope + structural validation
├── name_index.py        — thread-safe name → ID index (dedup gate)
├── entity_writer.py     — create / read / update / delete, atomic writes
├── snapshot.py          — in-memory + on-disk cache, O(1) freshness
├── db_registry.py       — named-database registry
├── validator.py         — semantic / cross-entity consistency checks
├── distiller.py         — LLM text → structured entity
├── expansion_engine.py  — grow the graph from stubs
├── vectorizer.py        — embeddings for similarity search
├── chunker.py           — chunk building for retrieval
├── baker.py             — export flattened snapshots
├── importer.py          — import entities from exported files
├── backup.py            — backup / restore
├── kind_registry.py     — fine-grained kind taxonomy
├── file_manifest.py     — file ↔ entity provenance
└── api_v1/              — versioned HTTP API (raw / baked / keys)
    ├── raw_routes.py        live CRUD, search, graph, batch
    ├── baked_routes.py      snapshot operations
    └── auth.py              API-key auth
```

---

## Documentation

- [Quickstart](docs/QUICKSTART.md) — install, run, first entity, import.
- [HTTP API reference](docs/API.md) — every endpoint, the response envelope, auth.
- [Library guide](docs/LIBRARY.md) — use the engine in-process.
- [Agents & the live feed](docs/AGENTS.md) — multiple agents working live.
- [Storage format](docs/STORAGE_FORMAT.md) — on-disk layout, versioning, concurrency.

---

## Roadmap

Done: typed entity store, dedup, snapshots, search, graph, import/export
(SQLite + `.snapshot`), baking, **realtime change feed** (live multi-agent
dashboard), versioned storage format, cross-process write safety, backups.

Toward a stable release:
- [ ] First tagged release on PyPI + `aethviondb` CLI
- [ ] Per-type schema / ontology enforcement
- [ ] MCP server — expose distill / upsert / search / graph as agent tools

The direction is to prove the engine through real use first, then package and
expose it more broadly — the same path Aethvion Project Mapper took.

---

## License

**Open-source core:** [GNU AGPL v3](LICENSE)
Free to use, modify, and self-host. Network use requires open-sourcing your
modifications.

**Commercial license:** [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)
Available for teams that need a proprietary license, SLA, or integration support.

---

*Built with care by the Aethvion team.*
