# AethvionDB — Library Guide

AethvionDB is usable in-process, without the HTTP server. The deterministic core
(entities, name index, graph, snapshots, validation, backup) has no LLM or
network dependency.

## Choosing a database

Each database is a directory. Point an `EntityWriter` at its `entities/` folder
and a `NameIndex` at its `name_index.json`. The default database is
`~/.aethvion/aethviondb/default/` (override the root with `AETHVIONDB_DATA_DIR`).

```python
from pathlib import Path
from aethviondb import EntityWriter, NameIndex

root = Path.home() / ".aethvion" / "aethviondb" / "mydb"
idx = NameIndex(index_path=root / "name_index.json")
w   = EntityWriter(entities_dir=root / "entities", index=idx)
```

> Pass the **per-database** `NameIndex` explicitly as shown — otherwise the
> writer uses the module-level default index.

## Create, read, update, delete

```python
entity, created = w.create("Ada Lovelace", entity_type="person")
ada_id = entity["id"]

w.update(ada_id, {"sections": {"core": {"summary": "Pioneer of computing.",
                                        "tags": ["mathematics", "computing"]}}})

w.get(ada_id)                 # by id
w.get_by_name("Ada Lovelace") # by name (via the index)

w.delete(ada_id)              # soft delete (status -> "deleted")
w.delete(ada_id, soft=False)  # hard delete (removes the file)
```

`create()` is get-or-create: the name index guarantees one record per name, even
under concurrent writers (across threads *and* processes).

## Optimistic concurrency

```python
from aethviondb import VersionConflictError

e = w.get(ada_id)
try:
    w.update(ada_id, {"sections": {"core": {"summary": "…"}}},
             expected_version=e["version"])
except VersionConflictError:
    e = w.get(ada_id)         # someone else wrote first — re-read and retry
```

## Relations & graph

```python
# relations are stored on the source entity, referencing target ids
w.update(ada_id, {"sections": {"relations": [
    {"kind": "created", "target_id": some_other_id, "note": ""}
]}})

for e in w.list_all():        # served from the in-memory snapshot cache
    ...
```

## Validation & backup

```python
from aethviondb import Validator
from aethviondb.backup import create_backup, restore_backup, list_backups

# Cross-entity consistency (duplicates, orphan stubs, broken relations, …)
v = Validator(writer=w)
one = v.validate(ada_id)        # one entity -> ValidationResult
allr = v.validate_all()         # whole database -> list[ValidationResult]

meta = create_backup(root, "mydb", label="before-merge")
restore_backup(root, meta["backup_id"])      # invalidates the cache
```

## Optional intelligence

Distillation / expansion / embedding activate only when a backend is injected:

```python
from aethviondb import ai_runtime
ai_runtime.set_llm_caller(my_caller)   # caller(prompt, *, system_prompt=None, ...) -> .content
```

Without a caller these features raise `LLMNotConfiguredError`; the deterministic
core is unaffected.

See [STORAGE_FORMAT.md](STORAGE_FORMAT.md) for the on-disk format and
[API.md](API.md) for the HTTP surface.
