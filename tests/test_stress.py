"""
tests/test_stress.py
Correctness at scale (P2-18). Opt in with: pytest --runslow

These build a few thousand entities and assert the engine stays correct under
size — not strict timing (that's what benchmarks/bench.py is for).
"""
from __future__ import annotations

import pytest

from aethviondb.entity_writer import EntityWriter
from aethviondb.importers.base import bulk_write, deterministic_id, make_entity
from aethviondb.name_index import NameIndex

pytestmark = pytest.mark.slow

N = 2000


def _writer(db_dir):
    (db_dir / "entities").mkdir(parents=True, exist_ok=True)
    return EntityWriter(entities_dir=db_dir / "entities",
                        index=NameIndex(index_path=db_dir / "name_index.json"))


def _bulk(db_dir, n):
    entities = [make_entity(entity_id=deterministic_id("n", i), name=f"Node {i:06d}",
                            kind="bench.node", source="stress",
                            summary=f"entity {i}", tags=["stress"],
                            relations=([{"kind": "related_to",
                                         "target_id": deterministic_id("n", i - 1), "note": ""}]
                                       if i else []))
                for i in range(n)]
    bulk_write(db_dir, entities)
    return entities


def test_bulk_load_and_read_at_scale(tmp_path):
    db_dir = tmp_path / "stress"
    w = _writer(db_dir)
    _bulk(db_dir, N)
    w.reindex()                                   # materialize snapshot + index

    assert len(w.list_all()) == N                 # cold read from snapshot
    assert len(w.list_all()) == N                 # warm read
    # name index works after reindex (sampled)
    for i in (0, N // 2, N - 1):
        assert w.get_by_name(f"Node {i:06d}") is not None


def test_lite_and_search_at_scale(tmp_path):
    from aethviondb import snapshot
    db_dir = tmp_path / "stress"
    w = _writer(db_dir)
    _bulk(db_dir, N)
    w.reindex()

    lite = snapshot.get_lite(db_dir, db_dir / "entities", w._raw_list_all)
    assert len(lite) == N
    assert all({"id", "name", "type", "status"} <= set(r) for r in lite[:5])

    # a keyword scan over the warm cache finds the right entity
    hits = [e for e in w.list_all() if "node 001000" in e["name"].lower()]
    assert len(hits) == 1


def test_writes_stay_consistent_at_scale(tmp_path):
    db_dir = tmp_path / "stress"
    w = _writer(db_dir)
    ents = _bulk(db_dir, N)
    w.reindex()

    target = ents[N // 3]["id"]
    for _ in range(5):
        w.update(target, {"sections": {"core": {"summary": "edited"}}})
    e = w.get(target)
    assert e["version"] == 6 and e["sections"]["core"]["summary"] == "edited"
    assert len(w.list_all()) == N                 # count unchanged by updates
