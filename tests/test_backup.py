"""
tests/test_backup.py
Backup / restore round-trips (P0-2): a backup captures a point in time, restore
reverts the database to it (entities + name index), and the cache reflects it.
"""
from __future__ import annotations

import pytest

from aethviondb.backup import (
    create_backup, list_backups, restore_backup, delete_backup, prune_backups,
)


class TestBackup:
    def test_create_and_list(self, entity_writer, db_dir):
        entity_writer.create("Alpha", entity_type="concept")
        entity_writer.create("Beta", entity_type="concept")
        meta = create_backup(db_dir, "test_db", label="snap one")
        assert meta["entity_count"] == 2
        assert meta["label"] == "snap one"
        backups = list_backups(db_dir)
        assert len(backups) == 1 and backups[0]["backup_id"] == meta["backup_id"]

    def test_restore_reverts_mutations_and_additions(self, entity_writer, db_dir):
        e, _ = entity_writer.create("Original", entity_type="concept")
        eid = e["id"]
        meta = create_backup(db_dir, "test_db")

        # Mutate the original and add a new entity after the backup.
        entity_writer.update(eid, {"sections": {"core": {"summary": "changed"}}})
        entity_writer.create("AddedAfter", entity_type="concept")
        assert entity_writer.get(eid)["sections"]["core"]["summary"] == "changed"

        report = restore_backup(db_dir, meta["backup_id"])
        assert report["restored"] is True and report["entity_count"] == 1

        # get() reads from disk (cache invalidated on restore) → original state.
        assert entity_writer.get(eid)["sections"]["core"].get("summary", "") == ""
        # list_all rebuilds from disk → the post-backup entity is gone.
        names = {e["name"] for e in entity_writer.list_all()}
        assert names == {"Original"}

        # Name index was restored too (reload mirrors a fresh process / API request).
        entity_writer._index.reload()
        assert entity_writer.get_by_name("AddedAfter") is None
        assert entity_writer.get_by_name("Original") is not None

    def test_delete_backup(self, entity_writer, db_dir):
        entity_writer.create("X", entity_type="concept")
        meta = create_backup(db_dir, "test_db")
        assert delete_backup(db_dir, meta["backup_id"]) is True
        assert list_backups(db_dir) == []
        assert delete_backup(db_dir, "nonexistent_id") is False

    def test_restore_missing_raises(self, db_dir):
        with pytest.raises(RuntimeError):
            restore_backup(db_dir, "no_such_backup")

    def test_prune_keeps_newest(self, entity_writer, db_dir):
        entity_writer.create("X", entity_type="concept")
        ids = [create_backup(db_dir, "test_db", label=f"b{i}")["backup_id"] for i in range(4)]
        # Distinct backup_ids required for prune to be meaningful.
        assert len(set(ids)) == 4
        deleted = prune_backups(db_dir, keep_count=2)
        assert len(deleted) == 2
        assert len(list_backups(db_dir)) == 2
