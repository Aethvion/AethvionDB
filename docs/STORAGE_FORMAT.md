# AethvionDB — Storage Format

This document describes the on-disk format of an AethvionDB database. **As of
v1.0.0 the format is committed:** the engine reads every prior `schema_version`,
and any change that alters the on-disk shape bumps the version and ships a
forward migration. Your data survives upgrades.

---

## Layout

A database is a single directory (default `~/.aethvion/aethviondb/<name>/`,
overridable with `AETHVIONDB_DATA_DIR`). Each named database is independent.

```
<db_root>/
├── entities/
│   └── ws_<hex>.json              # one file per entity — the durable source of truth
├── name_index.json                # name → entity-id index (dedup gate)
├── AethvionDB.SNAPSHOT            # warm-start cache: compact JSON array of all entities
├── AethvionDB.SNAPSHOT.meta.json  # { v, built_at, entity_count, built_gen, elapsed_ms }
├── AethvionDB.GEN                 # { "gen": N } — O(1) data-generation counter
├── AethvionDB.WRITELOCK          # cross-process write lock (filelock sidecar)
├── name_index.json.lock           # cross-process lock for the dedup gate
├── AethvionDB.VECINFO            # vectorization progress sidecar (optional)
├── settings.json                  # host settings (provider keys, default model) — see note
├── chunks/                        # retrieval chunk store (optional)
└── baked/                         # exported snapshots (jsonl / json / md / txt) + .meta
```

The **entity files are authoritative**. Everything else (`name_index.json`,
`AethvionDB.SNAPSHOT`, `AethvionDB.GEN`) is a derived cache and can be rebuilt
from the entity files; deleting them is safe (the engine regenerates them).

> Note: `settings.json` may hold provider API keys in plaintext (same trust level
> as a `.env`). Keep it out of backups you share, and off shared hosts until the
> commercial secret-store lands.

---

## The entity envelope

Every entity — from a person to a code function — shares one shape:

```jsonc
{
  "schema_version": 1,            // on-disk FORMAT version (see below)
  "id":      "ws_<16 hex>",       // stable, content-independent id
  "type":    "person|module|concept|...",   // coarse class (see VALID_TYPES)
  "kind":    "software.module",   // optional fine sub-type: string OR list of strings
  "name":    "Canonical Name",    // deduped by the name index; aliases in core.aliases
  "status":  "active|stub|deleted|planned|deprecated|experimental",
  "version": 1,                   // MUTATION counter — incremented on every write
  "created": "ISO-8601",
  "updated": "ISO-8601",
  "source":  "manual|import|expansion|distilled|<agent>",
  "sections": {
    "core":         { "summary": "", "aliases": [], "categories": [], "tags": [] },
    "timeline":     [ { "date": "...", "event": "...", "ref_ids": ["ws_..."] } ],
    "relations":    [ { "kind": "depends_on", "target_id": "ws_...", "note": "" } ],
    "properties":   { /* type-specific key/value facts */ },
    "stubs":        [ "Sub-topic that deserves its own entity" ],
    "vectors":      { "<model>": { "embedding": [...], "dimensions": N, ... } },
    "source_files": [ { "path": "", "hash": "", "lines": 0, "language": "", "size": 0 } ]
  }
}
```

### `schema_version` vs `version`
- **`schema_version`** — the format/shape of the stored JSON. Bumped only when the
  on-disk structure changes; drives migration. Current value: **1**.
- **`version`** — a per-entity mutation counter, incremented on every write and
  used for optimistic concurrency (the `If-Match` / `expected_version` 409 path).

These are independent: editing an entity 50 times gives `version: 51`,
`schema_version: 1`.

---

## Versioning & migration policy

- New entities are written with the current `schema_version`.
- Records written before versioning (no `schema_version` field) are structurally
  identical to v1 and are treated as v1; they are stamped the next time they are
  written (the write path calls `entity.setdefault("schema_version", …)`).
- `aethviondb.migrate(entity) -> (entity, changed)` upgrades a single record to
  the current version. It is the one place that knows how to move records
  forward; future format changes add a step per version bump.
- The engine **refuses to silently load a record whose `schema_version` is newer
  than it supports** (validation flags it) — so a newer file never gets
  misinterpreted by an older engine.

---

## Concurrency & write safety

Writes are safe both across threads and across processes — you can run a script
against a database while the server is also writing to it.

- **Within a process**: a per-entity `threading.Lock` makes each
  read-modify-write atomic, so concurrent edits to the same entity never lose an
  update (different entities proceed in parallel).
- **Across processes**: a per-database file lock (`AethvionDB.WRITELOCK`) wraps
  every mutation, and the name index takes its own file lock
  (`name_index.json.lock`) and re-reads from disk before mutating — so the
  dedup gate is correct across processes (concurrent `create("Foo")` from two
  processes yields one entity, not two).
- **Torn files**: every write is temp-file + atomic rename, so a reader never
  sees a half-written file even if a process is killed mid-write.
- **Optimistic concurrency**: pass `expected_version` (or the `If-Match` header)
  to a write; a stale version is rejected with `409` instead of clobbering a
  newer edit.

The `.lock` files are coordination sidecars — safe to delete when no process is
running.

## Rebuilding derived state

If the caches are ever stale or deleted:
- `name_index.json` — rebuilt by re-registering every entity's name (the snapshot
  importer does this via `NameIndex.register_many`).
- `AethvionDB.SNAPSHOT` / `.GEN` — rebuilt on next load from the entity files, or
  explicitly via `snapshot.build(db_root, entities)`.
