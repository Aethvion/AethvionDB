# Changelog

All notable changes to AethvionDB are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
