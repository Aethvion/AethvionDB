"""
tests/test_concurrency.py
Optimistic-concurrency control on EntityWriter.update().

Two guarantees are exercised:
  1. The version field advances by exactly one per successful update, and an
     ``expected_version`` that no longer matches is rejected (no silent clobber).
  2. The read-modify-write is atomic in-process: concurrent appends to a list
     section never lose an item, even though each is a load → mutate → write.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aethviondb.entity_writer import VersionConflictError

_ROOT = Path(__file__).resolve().parent.parent   # repo root, for subprocess imports


class TestOptimisticConcurrency:
    def test_version_increments_by_one(self, entity_writer):
        e, _ = entity_writer.create("Versioned", entity_type="concept")
        assert e["version"] == 1
        e2 = entity_writer.update(e["id"], {"sections": {"core": {"summary": "a"}}})
        assert e2["version"] == 2
        e3 = entity_writer.update(e["id"], {"sections": {"core": {"summary": "b"}}})
        assert e3["version"] == 3

    def test_matching_expected_version_succeeds(self, entity_writer):
        e, _ = entity_writer.create("MatchOK", entity_type="concept")
        updated = entity_writer.update(
            e["id"], {"sections": {"core": {"summary": "x"}}},
            expected_version=e["version"],
        )
        assert updated["version"] == e["version"] + 1

    def test_stale_expected_version_raises(self, entity_writer):
        e, _ = entity_writer.create("StaleEdit", entity_type="concept")
        # First writer advances the entity from v1 -> v2.
        entity_writer.update(e["id"], {"sections": {"core": {"summary": "first"}}})
        # Second writer still thinks it's on v1 -> must be rejected.
        with pytest.raises(VersionConflictError) as ei:
            entity_writer.update(
                e["id"], {"sections": {"core": {"summary": "second"}}},
                expected_version=1,
            )
        assert ei.value.expected == 1
        assert ei.value.actual == 2
        # The losing edit must not have been applied.
        assert entity_writer.get(e["id"])["sections"]["core"]["summary"] == "first"

    def test_concurrent_appends_lose_nothing(self, entity_writer):
        """N threads each append a unique timeline event; all must survive."""
        e, _ = entity_writer.create("HotEntity", entity_type="concept")
        eid = e["id"]
        n = 40
        start = threading.Barrier(n)

        def worker(i: int) -> None:
            start.wait()  # maximise contention
            entity_writer.update(
                eid, {"sections": {"timeline": [{"date": "2026", "event": f"e{i}"}]}}
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for tr in threads:
            tr.start()
        for tr in threads:
            tr.join()

        timeline = entity_writer.get(eid)["sections"]["timeline"]
        events = {item["event"] for item in timeline}
        assert events == {f"e{i}" for i in range(n)}, "an update was lost under contention"


# ── Cross-process safety (P0-3) ──
# Real separate processes hitting the same database — the only way to prove the
# cross-process FileLock works (threads share memory; a thread lock would "pass"
# even without it).

_UPDATE_WORKER = """
import sys
from pathlib import Path
from aethviondb.entity_writer import EntityWriter
from aethviondb.name_index import NameIndex
edir, tag, n, eid = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
idx = NameIndex(index_path=edir.parent / "name_index.json")
w = EntityWriter(entities_dir=edir, index=idx)
for i in range(n):
    w.update(eid, {"sections": {"timeline": [{"date": "2026", "event": f"{tag}-{i}"}]}})
"""

_CREATE_WORKER = """
import sys
from pathlib import Path
from aethviondb.entity_writer import EntityWriter
from aethviondb.name_index import NameIndex
edir = Path(sys.argv[1])
idx = NameIndex(index_path=edir.parent / "name_index.json")
w = EntityWriter(entities_dir=edir, index=idx)
w.create("Shared Name", entity_type="concept")
"""


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=str(_ROOT),                       # so `import aethviondb` resolves
    )


class TestCrossProcess:
    def test_no_lost_updates_across_processes(self, entity_writer, db_dir):
        e, _ = entity_writer.create("Hot", entity_type="concept")
        eid = e["id"]
        edir = str(db_dir / "entities")
        procs, per = 4, 12
        running = [_spawn(_UPDATE_WORKER, edir, f"p{k}", str(per), eid) for k in range(procs)]
        for p in running:
            assert p.wait(timeout=90) == 0, "worker process failed"

        raw = json.loads((db_dir / "entities" / f"{eid}.json").read_text(encoding="utf-8"))
        events = {t["event"] for t in raw["sections"]["timeline"]}
        expected = {f"p{k}-{i}" for k in range(procs) for i in range(per)}
        assert events == expected, "an update was lost across processes (file lock not holding)"

    def test_create_dedups_across_processes(self, db_dir):
        edir = str(db_dir / "entities")
        (db_dir / "entities").mkdir(parents=True, exist_ok=True)
        running = [_spawn(_CREATE_WORKER, edir) for _ in range(5)]
        for p in running:
            assert p.wait(timeout=90) == 0, "worker process failed"

        files = list((db_dir / "entities").glob("ws_*.json"))
        assert len(files) == 1, f"cross-process create made {len(files)} files; expected 1 (dedup)"
        index = json.loads((db_dir / "name_index.json").read_text(encoding="utf-8"))
        assert index.get("shared name") == files[0].stem
