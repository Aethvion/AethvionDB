# Changelog

All notable changes to AethvionDB are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] — toward 1.0.0

Hardening the engine into a stable, trustworthy release.

### Added
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
  version`.
- **Documentation set**: quickstart, API reference, library guide, agents guide.
- **Consistent API error envelope** and a request-size limit.
- Test suite expanded to ~80 tests (HTTP surface, concurrency incl. multi-process,
  schema versioning, backups, events).

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
