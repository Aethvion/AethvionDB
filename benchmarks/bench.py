"""
benchmarks/bench.py
Reproducible Layer-1 benchmark for AethvionDB.

Builds a throwaway database of N entities and times the core operations. Pure
engine (no HTTP), so it measures the storage layer itself.

    python benchmarks/bench.py            # default 30000 entities
    python benchmarks/bench.py 100000     # custom size

Prints a table; everything runs in a temp dir that is removed on exit.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from aethviondb import snapshot
from aethviondb.entity_writer import EntityWriter
from aethviondb.importers.base import bulk_write, deterministic_id, make_entity
from aethviondb.name_index import NameIndex


def _build_entities(n: int) -> list[dict]:
    out = []
    for i in range(n):
        rels = []
        if i > 0:
            rels = [{"kind": "related_to", "target_id": deterministic_id("node", i - 1), "note": ""}]
        out.append(make_entity(
            entity_id=deterministic_id("node", i),
            name=f"Node {i:06d}",
            kind="bench.node",
            source="bench",
            summary=f"Benchmark entity number {i}.",
            tags=["bench", f"bucket{i % 50}"],
            relations=rels,
        ))
    return out


def _timed(label: str, fn):
    t = time.perf_counter()
    result = fn()
    ms = (time.perf_counter() - t) * 1000
    print(f"  {label:<28} {ms:>10.2f} ms")
    return result, ms


def run(n: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="adb_bench_"))
    root = tmp / "benchdb"
    (root / "entities").mkdir(parents=True)
    idx = NameIndex(index_path=root / "name_index.json")
    w = EntityWriter(entities_dir=root / "entities", index=idx)

    print(f"\nAethvionDB Layer-1 benchmark - {n:,} entities")
    print("-" * 44)
    try:
        entities = _build_entities(n)
        _timed("bulk write (N files)", lambda: bulk_write(root, entities))
        _timed("reindex (snapshot + index)", w.reindex)

        # Cold load: drop the in-memory cache, then read back from the snapshot file.
        snapshot._MEM.pop(str(root), None)
        _timed("cold load (snapshot -> mem)", lambda: w.list_all())
        _timed("warm list_all", lambda: w.list_all())
        _timed("warm lite projection",
               lambda: snapshot.get_lite(root, root / "entities", w._raw_list_all))
        _timed("single create", lambda: w.create("Fresh One", entity_type="concept"))
        _timed("single update",
               lambda: w.update(entities[n // 2]["id"], {"sections": {"core": {"summary": "x"}}}))
        _timed("get_by_name (index)", lambda: w.get_by_name(f"Node {n // 2:06d}"))

        size_mb = sum(f.stat().st_size for f in (root / "entities").glob("ws_*.json")) / 1e6
        snap_mb = (snapshot.snapshot_path(root).stat().st_size / 1e6
                   if snapshot.snapshot_path(root).exists() else 0)
        print("-" * 44)
        print(f"  entity files: {size_mb:.1f} MB   snapshot: {snap_mb:.1f} MB")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 30000)
