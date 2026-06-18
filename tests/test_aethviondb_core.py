"""
tests/test_aethviondb_core.py
Layer-1 core tests for AethvionDB — the deterministic engine that must be
rock-solid before extraction:

  - Snapshot / in-memory cache + generation counter (incremental writes)
  - Entity lifecycle (update, soft/hard delete, reactivate)
  - Retrieval (list / filter / lite projection, search by type / kind / tag)

Layer-2 features (graph traversal, validate/repair, AI distill/expand) are
covered separately — they are optional extras, not the core contract.

All fixtures use isolated tmp directories (see conftest.py), and each test gets
a unique db root, so the process-global snapshot cache never bleeds between
tests.
"""
from __future__ import annotations

from aethviondb import snapshot


# ── Snapshot / in-memory cache + generation counter ─────────────────────────────

class TestSnapshotCache:
    def test_warm_list_matches_cold_list(self, entity_writer):
        entity_writer.create("Alpha", entity_type="concept")
        entity_writer.create("Beta", entity_type="person")
        cold = {e["id"] for e in entity_writer.list_all()}   # first call: cold load from files
        warm = {e["id"] for e in entity_writer.list_all()}   # second call: served from cache
        assert cold == warm
        assert len(cold) == 2

    def test_lite_projection_fields_and_count_match(self, entity_writer):
        entity_writer.create("Gamma", entity_type="concept",
                             sections_override={"core": {"summary": "hi", "tags": ["t1"]}})
        full = entity_writer.list_all()
        lite = entity_writer.list_lite()
        assert len(full) == len(lite)
        item = lite[0]
        for key in ("id", "name", "type", "kind", "status", "updated",
                    "summary", "tags", "relations_count", "stubs_count"):
            assert key in item, f"lite item missing {key!r}"

    def test_write_bumps_generation(self, entity_writer, db_dir):
        gen0 = snapshot.read_gen(db_dir)
        entity_writer.create("Delta", entity_type="concept")
        gen1 = snapshot.read_gen(db_dir)
        assert gen1 > gen0

    def test_new_entity_appears_after_warm_load(self, entity_writer):
        entity_writer.create("First", entity_type="concept")
        assert len(entity_writer.list_all()) == 1   # warms the cache
        entity_writer.create("Second", entity_type="concept")
        names = {e["name"] for e in entity_writer.list_all()}
        assert names == {"First", "Second"}          # cache was patched in place

    def test_soft_delete_excluded_by_default(self, entity_writer):
        e, _ = entity_writer.create("Doomed", entity_type="concept")
        entity_writer.create("Survivor", entity_type="concept")
        entity_writer.list_all()                     # warm
        entity_writer.delete(e["id"], soft=True)
        active = {x["name"] for x in entity_writer.list_all()}
        with_deleted = {x["name"] for x in entity_writer.list_all(include_deleted=True)}
        assert active == {"Survivor"}
        assert with_deleted == {"Doomed", "Survivor"}

    def test_hard_delete_removed_entirely(self, entity_writer):
        e, _ = entity_writer.create("Ephemeral", entity_type="concept")
        entity_writer.list_all()                     # warm
        entity_writer.delete(e["id"], soft=False)
        assert entity_writer.list_all(include_deleted=True) == []

    def test_count_active_vs_all(self, entity_writer):
        a, _ = entity_writer.create("A", entity_type="concept")
        entity_writer.create("B", entity_type="concept")
        entity_writer.delete(a["id"], soft=True)
        assert entity_writer.count(include_deleted=False) == 1
        assert entity_writer.count(include_deleted=True) == 2

    def test_flush_and_reload_from_disk(self, entity_writer, db_dir):
        entity_writer.create("Persist1", entity_type="concept")
        entity_writer.create("Persist2", entity_type="concept")
        entity_writer.list_all()                     # warm + populate cache
        snapshot.flush(db_dir)                        # persist snapshot to disk
        snapshot._MEM.clear()                         # simulate a fresh process
        reread = {e["name"] for e in entity_writer.list_all()}
        assert reread == {"Persist1", "Persist2"}

    def test_use_snapshot_false_bypasses_cache(self, entity_writer):
        entity_writer.create("Live", entity_type="concept")
        # A direct file read must agree with the cached view.
        assert len(entity_writer.list_all(use_snapshot=False)) == 1


# ── Entity lifecycle ────────────────────────────────────────────────────────────

class TestEntityLifecycle:
    def test_update_bumps_version_and_timestamp(self, entity_writer):
        e, _ = entity_writer.create("Versioned", entity_type="concept")
        assert e["version"] == 1
        updated = entity_writer.update(e["id"], {"sections": {"core": {"summary": "new"}}})
        assert updated["version"] == 2
        assert updated["sections"]["core"]["summary"] == "new"

    def test_update_merges_sections(self, entity_writer):
        e, _ = entity_writer.create(
            "Merged", entity_type="concept",
            sections_override={"core": {"summary": "orig", "tags": ["keep"]}},
        )
        updated = entity_writer.update(e["id"], {"sections": {"core": {"summary": "changed"}}})
        # summary replaced, but the pre-existing tag is preserved by the merge
        assert updated["sections"]["core"]["summary"] == "changed"
        assert "keep" in updated["sections"]["core"]["tags"]

    def test_soft_delete_then_recreate_reactivates(self, entity_writer):
        e1, _ = entity_writer.create("Phoenix", entity_type="concept")
        entity_writer.delete(e1["id"], soft=True)
        e2, created = entity_writer.create("Phoenix", entity_type="concept")
        assert e2["id"] == e1["id"]      # same identity reused
        assert created is True            # treated as fresh so children repopulate
        assert e2["status"] == "active"   # reactivated

    def test_get_after_update_returns_latest(self, entity_writer):
        e, _ = entity_writer.create("Fetch", entity_type="concept")
        entity_writer.update(e["id"], {"sections": {"core": {"summary": "v2"}}})
        loaded = entity_writer.get(e["id"])
        assert loaded["sections"]["core"]["summary"] == "v2"
        assert loaded["version"] == 2


# ── Retrieval / search ──────────────────────────────────────────────────────────

class TestRetrieval:
    def test_search_by_type(self, entity_writer):
        entity_writer.create("Person One", entity_type="person")
        entity_writer.create("Concept One", entity_type="concept")
        people = entity_writer.search_by_type("person")
        assert {e["name"] for e in people} == {"Person One"}

    def test_search_by_kind_string(self, entity_writer):
        entity_writer.create("Mod", entity_type="module", kind="software.module")
        entity_writer.create("Other", entity_type="concept")
        hits = entity_writer.search_by_kind("software.module")
        assert {e["name"] for e in hits} == {"Mod"}

    def test_search_by_kind_list(self, entity_writer):
        entity_writer.create("Multi", entity_type="module", kind=["a.b", "c.d"])
        assert {e["name"] for e in entity_writer.search_by_kind("c.d")} == {"Multi"}

    def test_search_by_tag_case_insensitive(self, entity_writer):
        entity_writer.create("Tagged", entity_type="concept",
                             sections_override={"core": {"tags": ["Physics", "Math"]}})
        assert {e["name"] for e in entity_writer.search_by_tag("physics")} == {"Tagged"}

    def test_search_excludes_deleted(self, entity_writer):
        e, _ = entity_writer.create("GoneType", entity_type="person")
        entity_writer.delete(e["id"], soft=True)
        assert entity_writer.search_by_type("person") == []
