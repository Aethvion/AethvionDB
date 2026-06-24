"""
tests/test_schema_version.py
Storage-format versioning (P0-1): every persisted entity carries schema_version,
legacy records are stamped on write, migrate() upgrades, and validation rejects
records from a newer engine.
"""
from __future__ import annotations

import json

from aethviondb.entity_schema import SCHEMA_VERSION, make_empty, migrate, validate


class TestSchemaVersion:
    def test_make_empty_stamps_current_version(self):
        e = make_empty("Alpha", entity_type="concept")
        assert e["schema_version"] == SCHEMA_VERSION

    def test_created_entity_has_schema_version_on_disk(self, entity_writer, db_dir):
        e, _ = entity_writer.create("Disk Versioned", entity_type="concept")
        raw = json.loads((db_dir / "entities" / f"{e['id']}.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == SCHEMA_VERSION

    def test_version_and_schema_version_are_independent(self, entity_writer):
        e, _ = entity_writer.create("Independent", entity_type="concept")
        for _ in range(3):
            e = entity_writer.update(e["id"], {"sections": {"core": {"summary": "x"}}})
        assert e["version"] == 4               # mutation counter advanced
        assert e["schema_version"] == SCHEMA_VERSION   # format version unchanged

    def test_legacy_entity_stamped_on_write(self, entity_writer, db_dir):
        # Create, then strip schema_version on disk to simulate a pre-versioning record.
        e, _ = entity_writer.create("Legacy", entity_type="concept")
        path = db_dir / "entities" / f"{e['id']}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["schema_version"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        # Next write must re-stamp it.
        entity_writer.update(e["id"], {"sections": {"core": {"summary": "touched"}}})
        raw2 = json.loads(path.read_text(encoding="utf-8"))
        assert raw2["schema_version"] == SCHEMA_VERSION

    def test_migrate_stamps_missing_and_is_idempotent(self):
        legacy = {"id": "ws_x", "name": "L", "sections": {}}
        out, changed = migrate(legacy)
        assert changed is True and out["schema_version"] == 1
        out2, changed2 = migrate(out)
        assert changed2 is False             # already current → no change

    def test_validate_rejects_future_version(self):
        e = make_empty("FromFuture", entity_type="concept")
        e["schema_version"] = SCHEMA_VERSION + 1
        errors = validate(e)
        assert any("newer than this engine" in err for err in errors)

    def test_validate_rejects_non_int_version(self):
        e = make_empty("BadVer", entity_type="concept")
        e["schema_version"] = "1"
        errors = validate(e)
        assert any("must be an integer" in err for err in errors)
