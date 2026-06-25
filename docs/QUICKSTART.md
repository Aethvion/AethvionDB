# AethvionDB — Quickstart

Get a knowledge database running and write your first entity in a couple of
minutes.

## Install

```bash
# From a checkout (editable)
pip install -e ".[dev]"

# Optional extras (all features work without these):
pip install -e ".[embeddings]"   # local embeddings (sentence-transformers)
pip install -e ".[openai]"       # OpenAI embeddings
pip install -e ".[google]"       # Google embeddings
```

Requires Python 3.10+.

## Run the server + dashboard

```bash
aethviondb-server
#   dashboard  → http://127.0.0.1:7475/
#   API docs   → http://127.0.0.1:7475/docs
#   API base   → http://127.0.0.1:7475/api/v1
```

Data lives under `~/.aethvion/aethviondb/` by default. Override with
`AETHVIONDB_DATA_DIR`. The server binds to `127.0.0.1`; set `AETHVIONDB_HOST` /
`AETHVIONDB_PORT` to change.

The **dashboard** is the fastest way to explore: browse/search entities, view the
graph, import databases, bake snapshots, manage backups, and watch the live
change feed as writes happen.

## Your first entity (HTTP)

```bash
curl -X POST http://127.0.0.1:7475/api/v1/default/raw/entities/upsert \
  -H "Content-Type: application/json" \
  -d '{"name":"Ada Lovelace","type":"person","summary":"Pioneer of computing."}'

curl http://127.0.0.1:7475/api/v1/default/raw/entities   # list
```

## Your first entity (library)

```python
from aethviondb import EntityWriter

w = EntityWriter()                       # default database
entity, created = w.create("Ada Lovelace", entity_type="person")
print(entity["id"], created)
print(w.get_by_name("Ada Lovelace")["id"])
```

## Load real data (import)

The fastest way to fill a database is to import an existing one. From the
dashboard's **Import** page, pick a folder; it's scanned recursively for SQLite
(`.db/.sqlite/.sqlite3`) and AethvionDB snapshot (`.snapshot`) files, and you
choose which to import. Tables become entity *kinds*, rows become entities, and
foreign keys become typed relations.

Or via the API:

```bash
curl -X POST http://127.0.0.1:7475/api/import/run \
  -H "Content-Type: application/json" \
  -d '{"source_type":"sqlite","source":"C:/path/to/chinook.db","db":"chinook"}'
```

## Next

- [API reference](API.md) — every endpoint.
- [Library guide](LIBRARY.md) — use the engine in-process.
- [Agents & the live feed](AGENTS.md) — multiple agents working live.
- [Storage format](STORAGE_FORMAT.md) — on-disk layout & guarantees.
