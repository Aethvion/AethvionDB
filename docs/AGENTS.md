# AethvionDB — Agents & the Live Feed

AethvionDB is built to be a **shared brain** that many agents read and write at
once. Agents talk to it over the HTTP API; every write is broadcast on a live
change feed so other agents (and the dashboard) see it immediately.

## How an agent connects

Any HTTP client works. An agent should:

1. Send `X-Actor: <agent-name>` on writes, so its changes are attributed.
2. Send `X-API-Key: <key>` if the database requires auth.
3. Use **upsert** (get-or-create by name) to avoid duplicates, and pass
   `expected_version` on edits it wants to be conflict-safe.

```python
import httpx

BASE = "http://127.0.0.1:7475/api/v1"
DB   = "shared"
H    = {"X-Actor": "coding-agent"}   # add "X-API-Key": "…" if keys are set

c = httpx.Client(base_url=BASE, headers=H)

# write a fact
c.post(f"/{DB}/raw/entities/upsert", json={
    "name": "PaymentService",
    "type": "service",
    "summary": "Handles checkout and refunds.",
    "relations": [{"kind": "depends_on", "target_name": "PostgresDB"}],
})

# read it back
hits = c.post(f"/{DB}/raw/search", json={"query": "payment", "modes": ["keyword"]}).json()
print(hits["data"]["results"][0]["name"])

# traverse the graph
svc = c.get(f"/{DB}/raw/entities/lite").json()["data"]["rows"]
sid = next(r["id"] for r in svc if r["name"] == "PaymentService")
graph = c.post(f"/{DB}/raw/graph/traverse", json={"start_id": sid, "depth": 2, "direction": "both"}).json()
```

## Watching the live feed (SSE)

Subscribe to `GET /{db}/events` (Server-Sent Events). Each event is one JSON line:
`{action, id, name, entity_type, kind, version, actor, ts}` where `action` is
`created | updated | deleted`.

```python
import json, httpx

def watch(db: str, base="http://127.0.0.1:7475/api/v1", key: str | None = None):
    url = f"{base}/{db}/events" + (f"?key={key}" if key else "")
    with httpx.stream("GET", url, timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                print(f"{ev['actor']} {ev['action']} {ev['name']}")

watch("shared")
# → doc-agent updated PaymentService
# → coding-agent created RefundJob
```

A typical multi-agent loop: each agent **watches** the feed to stay aware of what
others change, and **writes** its own findings back — so knowledge discovered by
one agent is instantly usable by all.

## Concurrency & safety guarantees

- **No duplicates**: `create`/`upsert` dedupe by name across threads *and*
  processes (the name index is cross-process locked).
- **No lost updates**: each write is an atomic read-modify-write under a
  cross-process lock; pass `expected_version` to get a `409` instead of
  clobbering a newer edit.
- **No torn reads**: writes are atomic (temp-file + rename).

See [API.md](API.md) for the full surface and [STORAGE_FORMAT.md](STORAGE_FORMAT.md)
for the on-disk guarantees.
