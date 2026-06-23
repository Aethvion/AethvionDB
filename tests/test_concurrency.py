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

import threading

import pytest

from aethviondb.entity_writer import VersionConflictError


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
