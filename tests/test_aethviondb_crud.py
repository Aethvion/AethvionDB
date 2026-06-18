"""
tests/test_aethviondb_crud.py
Unit tests for AethvionDB entity CRUD operations:
  - NameIndex  (name → ID lookup, dedup, normalization)
  - EntityWriter (create, get, update)
"""
from __future__ import annotations



# NameIndex

class TestNameIndex:
    def test_empty_index_returns_none(self, name_index):
        assert name_index.get("Albert Einstein") is None

    def test_register_and_get(self, name_index):
        name_index.register("Albert Einstein", "ws_ae001")
        assert name_index.get("Albert Einstein") == "ws_ae001"

    def test_normalization_case_insensitive(self, name_index):
        name_index.register("Marie Curie", "ws_mc001")
        assert name_index.get("marie curie") == "ws_mc001"
        assert name_index.get("MARIE CURIE") == "ws_mc001"

    def test_normalization_whitespace(self, name_index):
        name_index.register("Isaac  Newton", "ws_in001")
        # Collapsed internal whitespace should resolve to the same entry
        assert name_index.get("Isaac Newton") == "ws_in001"

    def test_get_or_create_new(self, name_index):
        eid, created = name_index.get_or_create("Nikola Tesla", "ws_nt001")
        assert eid == "ws_nt001"
        assert created is True

    def test_get_or_create_existing(self, name_index):
        name_index.register("Nikola Tesla", "ws_nt001")
        eid, created = name_index.get_or_create("Nikola Tesla", "ws_nt_ignored")
        assert eid == "ws_nt001"  # returns existing ID
        assert created is False

    def test_persistence_across_instances(self, db_dir):
        """Index written to disk should survive a new NameIndex instance."""
        from aethviondb.name_index import NameIndex
        idx_path = db_dir / "name_index.json"

        idx1 = NameIndex(index_path=idx_path)
        idx1.register("Galileo", "ws_gal001")

        idx2 = NameIndex(index_path=idx_path)
        assert idx2.get("Galileo") == "ws_gal001"


# EntityWriter

class TestEntityWriter:
    def test_create_returns_entity_and_was_created_true(self, entity_writer):
        entity, created = entity_writer.create("Test Entity", entity_type="person")
        assert created is True
        assert entity["name"] == "Test Entity"
        assert entity["type"] == "person"
        assert "id" in entity

    def test_create_duplicate_returns_existing(self, entity_writer):
        e1, _ = entity_writer.create("Duplicate", entity_type="concept")
        e2, created = entity_writer.create("Duplicate", entity_type="concept")
        assert created is False
        assert e1["id"] == e2["id"]

    def test_get_nonexistent_returns_none(self, entity_writer):
        assert entity_writer.get("ws_nonexistent") is None

    def test_get_returns_persisted_entity(self, entity_writer):
        e, _ = entity_writer.create("Persisted Entity", entity_type="location")
        loaded = entity_writer.get(e["id"])
        assert loaded is not None
        assert loaded["name"] == "Persisted Entity"

    def test_get_by_name(self, entity_writer):
        e, _ = entity_writer.create("Named Lookup", entity_type="event")
        loaded = entity_writer.get_by_name("Named Lookup")
        assert loaded is not None
        assert loaded["id"] == e["id"]

    def test_exists_true_after_create(self, entity_writer):
        e, _ = entity_writer.create("Existence Check", entity_type="object")
        assert entity_writer.exists(e["id"]) is True

    def test_exists_false_for_unknown(self, entity_writer):
        assert entity_writer.exists("ws_unknown_xyz") is False

    def test_created_entity_has_timestamps(self, entity_writer):
        e, _ = entity_writer.create("Timestamped", entity_type="concept")
        # Schema uses "created" and "updated" (not "created_at" / "updated_at")
        assert e.get("created") is not None
        assert e.get("updated") is not None

    def test_entity_file_written_to_disk(self, entity_writer, db_dir):
        e, _ = entity_writer.create("Disk Write", entity_type="person")
        entity_file = db_dir / "entities" / f"{e['id']}.json"
        assert entity_file.exists()
