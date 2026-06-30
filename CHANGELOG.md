# Changelog

All notable changes to AethvionDB are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0rc1] — 2026-06-30

Release candidate: the engine hardened into a stable, trustworthy release with a
committed storage format, cross-process safety, a tested public surface, and the
full agent/dashboard experience.

### Added (agent & tooling layer)
- **`AethvionClient`** — a dependency-free (stdlib) Python client: CRUD, search,
  graph, validate, reindex, backup, and an auto-reconnecting `watch()` that
  replays missed live-feed events.
- **MCP server** (`layer2/aethviondb-mcp`) — 11 tools exposing the knowledge base
  to agents (Claude Desktop, Cursor), built on `AethvionClient`.
- **Per-type ontology** — `KindRegistry.required_properties` enforced softly by
  the validator and surfaced in the Health view; API + dashboard to manage kinds.
- **Database management** — create / delete / rename databases (API + dashboard).
- **Reproducible benchmark** (`benchmarks/bench.py`) + opt-in large-DB stress
  tests (`pytest --runslow`).

### Added (core hardening)
- **Versioned storage format**: `schema_version` on every entity, a `migrate()`
  seam, and a committed on-disk format (`docs/STORAGE_FORMAT.md`).
- **Cross-process write safety**: per-database file lock around writes and a
  cross-process, reload-before-mutate name index — safe to write from a script
  while the server runs.
- **Backups**: create / list / restore / delete via API, CLI, and dashboard.
- **Live change feed**: Server-Sent Events (`GET /{db}/events`) with `X-Actor`
  attribution; the dashboard updates live as agents write.
- **Import / export**: folder-scan multi-select SQLite import with live progress,
  and AethvionDB `.snapshot` import/export for full-database round-trips.
- **Health view**: `GET /{db}/raw/validate` + a dashboard Health page surfacing
  duplicates, broken relations, orphan stubs, warnings, and soft-deleted records.
- **Capabilities + settings**: `/capabilities`, `/settings` (masked provider
  keys) and a dashboard to enable features without a restart.
- **Virtualized explorer** for databases with tens of thousands of entities.
- **`aethviondb` CLI**: `serve / init / backup / backups / restore / validate /
  reindex / version`.
- **Documentation set**: quickstart, API reference, library guide, agents guide.
- **Consistent API error envelope** and a request-size limit.
- Test suite expanded to ~100 tests (HTTP surface, concurrency incl.
  multi-process, schema versioning, backups, events, client, MCP) plus opt-in
  large-DB stress tests.

### Changed
- `filelock` added as a core dependency (cross-process safety).
- API errors now return `{ok:false, error, detail, meta}` (success unchanged).

---

## [0.1.0] — 2026-06-17

First standalone release — the engine extracted from Aethvion Suite into its own
package, with zero dependency on the host application.

### Added
- **Layer-1 deterministic core**: typed entity store (one JSON file per entity),
  name-index deduplication, in-memory + on-disk snapshot cache with an O(1)
  generation-counter freshness check, schema validation, keyword/type/kind/tag
  retrieval, import/bake/backup.
- **Versioned HTTP API** (`/api/v1`): raw CRUD + batch + upsert, baked snapshots,
  per-database API keys, hybrid + vector search, graph traverse/neighbors/path.
- **Optional, injected intelligence (Layer 2)**: distill / expand / deepen and
  embedding generation activate only when an LLM/embedding backend is injected
  (`aethviondb.ai_runtime.set_llm_caller`); the core runs without one.
- **Standalone server** (`aethviondb-server`) and library API.
- Test suite (34 tests) and a reproducible Layer-1 benchmark.

### Notes
- Default data directory is `~/.aethvion/aethviondb` (override with
  `AETHVIONDB_DATA_DIR`). No backwards-compatibility shims — clean start.
