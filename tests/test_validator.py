"""
tests/test_validator.py
Direct unit tests for the validator checks (P-A.4). The aggregate path is covered
via the API (test_api); these pin the individual rule behaviours.
"""
from __future__ import annotations

from aethviondb.entity_schema import make_empty
from aethviondb.validator import (
    Severity, Validator,
    _check_self_reference, _check_type_consistency, _check_reference_integrity,
    _check_containment_cycle, _check_kind_schema,
)


class TestChecks:
    def test_self_reference_is_error(self):
        e = make_empty("A", "concept")
        e["sections"]["relations"] = [{"kind": "related_to", "target_id": e["id"], "note": ""}]
        issues = _check_self_reference(e)
        assert len(issues) == 1
        assert issues[0].check == "self_reference" and issues[0].severity == Severity.ERROR

    def test_type_consistency_empty_summary_warns(self):
        e = make_empty("A", "concept")            # active, no summary
        assert any(i.check == "type_consistency" and "summary" in i.message
                   for i in _check_type_consistency(e))

    def test_type_consistency_event_needs_timeline(self):
        e = make_empty("Launch", "event")
        e["sections"]["core"]["summary"] = "An event."
        assert any("timeline" in i.message for i in _check_type_consistency(e))

    def test_reference_integrity(self, entity_writer):
        a, _ = entity_writer.create("A", entity_type="concept")
        a["sections"]["relations"] = [{"kind": "depends_on", "target_id": "ws_ghost", "note": ""}]
        issues = _check_reference_integrity(a, entity_writer)
        assert any(i.severity == Severity.ERROR and i.check == "reference_integrity" for i in issues)
        # a valid target produces no issue
        b, _ = entity_writer.create("B", entity_type="concept")
        a["sections"]["relations"] = [{"kind": "depends_on", "target_id": b["id"], "note": ""}]
        assert _check_reference_integrity(a, entity_writer) == []

    def test_containment_cycle(self, entity_writer):
        a, _ = entity_writer.create("A", entity_type="concept")
        b, _ = entity_writer.create("B", entity_type="concept")
        entity_writer.update(a["id"], {"sections": {"relations": [{"kind": "part_of", "target_id": b["id"], "note": ""}]}})
        entity_writer.update(b["id"], {"sections": {"relations": [{"kind": "part_of", "target_id": a["id"], "note": ""}]}})
        issues = _check_containment_cycle(entity_writer.get(a["id"]), entity_writer)
        assert any(i.check == "containment_cycle" for i in issues)

    def test_kind_schema_required_property(self):
        e = make_empty("M", "module", kind="software.module")
        defs = {"software.module": {"name": "software.module", "required_properties": ["language"]}}
        assert any(i.check == "kind_schema" for i in _check_kind_schema(e, defs))
        e["sections"]["properties"] = {"language": "python"}
        assert _check_kind_schema(e, defs) == []

    def test_kind_schema_skips_inactive(self):
        e = make_empty("M", "module", kind="software.module", status="stub")
        defs = {"software.module": {"name": "software.module", "required_properties": ["language"]}}
        assert _check_kind_schema(e, defs) == []   # only active entities are enforced


class TestAggregate:
    def test_clean_entity_has_no_errors(self, entity_writer):
        e, _ = entity_writer.create("Clean", entity_type="concept")
        entity_writer.update(e["id"], {"sections": {"core": {"summary": "A clear description."}}})
        assert Validator(writer=entity_writer).validate(e["id"]).ok

    def test_duplicate_detection_via_alias(self, entity_writer):
        entity_writer.create("Alpha", entity_type="concept")
        b, _ = entity_writer.create("Beta", entity_type="concept")
        entity_writer.update(b["id"], {"sections": {"core": {"aliases": ["Alpha"]}}})  # collides with "Alpha"
        results = Validator(writer=entity_writer).validate_all()
        dups = [i for r in results for i in r.issues if i.check == "duplicate_entity"]
        assert dups, "expected a duplicate_entity issue for the alias collision"
