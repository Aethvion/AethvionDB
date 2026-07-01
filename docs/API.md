# AethvionDB — HTTP API Reference

Base URL: `http://127.0.0.1:7475/api/v1`. Interactive docs (OpenAPI) at `/docs`.

## Conventions

**Response envelope.** Every response shares one shape.

```jsonc
// success
{ "ok": true, "data": { ... }, "meta": { "version": "v1", "db": "default", "took_ms": 1.2, "cursor": "…" } }
// error
{ "ok": false, "error": { "code": "not_found", "message": "…" }, "detail": "…", "meta": { "version": "v1" } }
```

`meta.cursor` (when present) is an opaque pagination cursor — pass it back as
`?cursor=`. `error.code` is one of `bad_request, unauthorized, forbidden,
not_found, conflict, payload_too_large, validation_error, internal_error`.

**Auth.** Open by default. Once a database has keys, send `X-API-Key: <key>`
(the browser SSE endpoint also accepts `?key=`). Manage keys under `/{db}/keys`.

**Attribution.** Send `X-Actor: <name>` on writes (e.g. `coding-agent`); it's
recorded on the change event and falls back to the entity's `source`.

**Concurrency.** Pass `expected_version` (body) or `If-Match: <version>` to a
`PATCH`; a stale version returns `409` with the current version so you can rebase.

**Limits.** Request bodies are capped (`AETHVIONDB_MAX_BODY_BYTES`, default 64 MB → `413`).

---

## Discovery

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | API version + list of databases |
| GET | `/capabilities` | Per-feature status (installed / configured / ready) |
| GET | `/settings` | Host settings (provider keys **masked**) |
| PUT | `/settings` | Update keys / default embedding model (empty keys ignored) |
| GET | `/health` (root, not under `/api/v1`) | Liveness |

## Entities — `/{db}/raw/...`

| Method | Path | Notes |
|---|---|---|
| GET | `/entities` | List. Query: `status` (active/all/stub/deleted/…), `type`, `kind`, `limit`≤500, `cursor`, `sections` (csv). Embeddings stripped. |
| GET | `/entities/lite` | **All** matching rows, compact projection (id/name/type/kind/status/relations_count). For virtualized lists. |
| GET | `/entities/{id}` | Full entity. `sections` (csv) projects. `404` if missing. |
| POST | `/entities/upsert` | Create or update by name. Body: `name, type, kind, status, source, summary, aliases[], tags[], categories[], properties{}, relations[]`. Returns `{entity, action}`. |
| PATCH | `/entities/{id}` | Partial update. Body: `mutations{}`, optional `expected_version`. `409` on stale version. |
| DELETE | `/entities/{id}` | Soft delete (status→deleted). `?hard=true` removes the file. |
| POST | `/entities/batch` | `{operations:[{op:upsert|patch|delete, …}], atomic?}`. |
| GET | `/entities/{id}/relations` | Relations with resolved target names. |
| GET | `/entities/{id}/vectors` | Stored embeddings for the entity. |
| GET | `/entities/{id}/timeline` | Timeline events. |

`relations[]` items: `{kind, target_name?|target_id?, note?}` — unknown
`target_name` is created as a stub so the edge isn't lost.

## Search — `/{db}/raw/...`

| Method | Path | Body |
|---|---|---|
| POST | `/search` | `{query, modes:["keyword"\|"vector"], vector_model?, filters{}, limit, min_score, cursor}` → ranked `results`. |
| POST | `/vectors/search` | `{query?\|vector?, model, top_k, filters{}}` → similarity `results`. |

## Graph — `/{db}/raw/...`

| Method | Path | Body / notes |
|---|---|---|
| POST | `/graph/traverse` | `{start_id, depth, direction:outbound\|inbound\|both, relation_kinds?, limit, return_paths?}` → `{nodes, edges}`. |
| GET | `/graph/neighbors/{id}` | `?direction=` → `{inbound, outbound}`. |
| POST | `/graph/path` | `{start_id, end_id, max_depth}` → `{found, length, path, nodes}`. |

## Intelligence (optional) — `/{db}/raw/...`

| Method | Path | Notes |
|---|---|---|
| POST | `/distill` | LLM text → entity. Enabled by an OpenAI/Google key in Settings (or a host-injected caller); `400` if none. |
| GET | `/vectorize/models` | Embedding models (local + API). |
| GET | `/vectorize/status` | Progress of a vectorization pass. |
| POST | `/vectorize` | Start embedding all entities. `{model, force_rewrite?, include_stubs?}`. |
| POST | `/vectorize/cancel` | Cancel a running pass. |

## Snapshots, backups, live feed — `/{db}/raw/...` & `/{db}/...`

| Method | Path | Notes |
|---|---|---|
| GET | `/{db}/raw/snapshot/download` | Export the whole db as a portable `<db>.snapshot` (JSON array). |
| GET | `/{db}/events` | **Server-Sent Events** change feed. `?key=` for auth. Each event: `{action, id, name, entity_type, kind, version, actor, ts}`. |
| POST | `/{db}/raw/reindex` | Rebuild snapshot + name index from the entity files (warm-up / repair). |
| GET | `/{db}/raw/validate` | Consistency report (errors, duplicates, orphan stubs, warnings, soft-deleted). |
| GET/POST | `/{db}/backups` | List / create (`{label}`) point-in-time backups. |
| POST | `/{db}/backups/{id}/restore` | Restore (replaces contents; cache invalidated). |
| DELETE | `/{db}/backups/{id}` | Delete a backup. |

## Baked snapshots — `/{db}/baked/...`

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/{db}/baked` | List / trigger a bake (`{name, format:jsonl\|json\|markdown\|txt, include_stubs?, include_vectors?, vector_models?}`). |
| GET/DELETE/PATCH | `/{db}/baked/{name}` | Get meta / delete / rename (`{new_name}`). |
| GET | `/{db}/baked/{name}/entities` | Paginated entities from the snapshot. |
| POST | `/{db}/baked/{name}/search` | Keyword search within the snapshot (jsonl/json only). |
| GET | `/{db}/baked/{name}/download` | Download the baked file. |

## API keys — `/{db}/keys`

| Method | Path | Notes |
|---|---|---|
| GET | `/{db}/keys` | List labels + `open_access`. |
| POST | `/{db}/keys` | `{label, scopes:["read","write"]}` → the key (**shown once**). |
| DELETE | `/{db}/keys/{label}` | Revoke. |

## Import — `/api/import/...`

| Method | Path | Notes |
|---|---|---|
| GET | `/sources` | Source types + matched extensions. |
| POST | `/scan` | `{source_type, folder}` → matching files (recursive). |
| POST | `/pick-folder` | Opens a native folder picker on the host; returns the path. |
| POST | `/preview` | `{source_type, source, db}` → what would be imported. |
| POST | `/run` | `{source_type, source, db}` → import summary. `source_type`: `sqlite`, `snapshot`. |
