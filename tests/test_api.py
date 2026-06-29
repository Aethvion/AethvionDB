"""
tests/test_api.py
HTTP surface tests (P0-4) — exercise the public /api/v1 + /api/import endpoints
through a FastAPI TestClient: CRUD, optimistic-concurrency 409, search, graph,
lite list, batch, snapshot export/import round-trip, and backups.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_DB = _ROOT / "benchmark_databases" / "sample.db"


def _data(resp):
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True, body
    return body["data"]


def _upsert(client, db, **fields):
    return _data(client.post(f"/api/v1/{db}/raw/entities/upsert", json=fields))


# ── Discovery ──

def test_index_lists_databases(client):
    d = _data(client.get("/api/v1/"))
    assert d["version"] == "v1" and isinstance(d["databases"], list)


# ── CRUD ──

def test_upsert_create_get_list(client, db_name):
    res = _upsert(client, db_name, name="Ada Lovelace", type="person", summary="Pioneer.")
    assert res["action"] == "created"
    eid = res["entity"]["id"]
    assert res["entity"]["schema_version"] >= 1   # P0-1 stamped

    got = _data(client.get(f"/api/v1/{db_name}/raw/entities/{eid}"))
    assert got["name"] == "Ada Lovelace"

    lst = _data(client.get(f"/api/v1/{db_name}/raw/entities"))
    assert lst["total"] == 1 and lst["entities"][0]["id"] == eid


def test_upsert_updates_existing_and_bumps_version(client, db_name):
    first = _upsert(client, db_name, name="Dup", type="concept")
    assert first["action"] == "created" and first["entity"]["version"] == 1
    second = _upsert(client, db_name, name="Dup", type="concept", summary="now with summary")
    assert second["action"] == "updated"
    assert second["entity"]["id"] == first["entity"]["id"]
    assert second["entity"]["version"] == 2


def test_patch_optimistic_concurrency_409(client, db_name):
    eid = _upsert(client, db_name, name="Guarded")["entity"]["id"]
    ok = client.patch(f"/api/v1/{db_name}/raw/entities/{eid}",
                      json={"mutations": {"sections": {"core": {"summary": "v2"}}}, "expected_version": 1})
    assert ok.status_code == 200
    stale = client.patch(f"/api/v1/{db_name}/raw/entities/{eid}",
                         json={"mutations": {"sections": {"core": {"summary": "boom"}}}, "expected_version": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "version_conflict"


def test_delete_soft_then_hard(client, db_name):
    eid = _upsert(client, db_name, name="Doomed")["entity"]["id"]
    soft = _data(client.delete(f"/api/v1/{db_name}/raw/entities/{eid}"))
    assert soft["mode"] == "soft"
    # soft-deleted: gone from default (active) list, present with status=deleted
    assert _data(client.get(f"/api/v1/{db_name}/raw/entities"))["total"] == 0
    assert _data(client.get(f"/api/v1/{db_name}/raw/entities?status=deleted"))["total"] == 1
    hard = _data(client.delete(f"/api/v1/{db_name}/raw/entities/{eid}?hard=true"))
    assert hard["mode"] == "hard"
    assert client.get(f"/api/v1/{db_name}/raw/entities/{eid}").status_code == 404


def test_get_missing_returns_404(client, db_name):
    _upsert(client, db_name, name="Exists")   # ensure db exists
    assert client.get(f"/api/v1/{db_name}/raw/entities/ws_nope").status_code == 404


# ── Search / lite ──

def test_keyword_search(client, db_name):
    _upsert(client, db_name, name="Daft Punk", type="other")
    _upsert(client, db_name, name="Aphex Twin", type="other")
    d = _data(client.post(f"/api/v1/{db_name}/raw/search", json={"query": "daft", "modes": ["keyword"]}))
    assert d["results"] and d["results"][0]["name"] == "Daft Punk"


def test_lite_list(client, db_name):
    for i in range(5):
        _upsert(client, db_name, name=f"Node {i}", type="concept")
    d = _data(client.get(f"/api/v1/{db_name}/raw/entities/lite"))
    assert d["total"] == 5
    assert set(d["rows"][0].keys()) >= {"id", "name", "type", "status", "relations_count"}


# ── Graph ──

def test_graph_traverse_and_path(client, db_name):
    a = _upsert(client, db_name, name="A", relations=[{"kind": "related_to", "target_name": "B"}])["entity"]
    b = client.get(f"/api/v1/{db_name}/raw/entities/lite").json()["data"]["rows"]
    bid = next(r["id"] for r in b if r["name"] == "B")

    trav = _data(client.post(f"/api/v1/{db_name}/raw/graph/traverse",
                             json={"start_id": a["id"], "depth": 2, "direction": "both"}))
    assert trav["node_count"] >= 2 and trav["edge_count"] >= 1

    nb = _data(client.get(f"/api/v1/{db_name}/raw/graph/neighbors/{a['id']}"))
    assert any(o["id"] == bid for o in nb["outbound"])

    path = _data(client.post(f"/api/v1/{db_name}/raw/graph/path",
                             json={"start_id": a["id"], "end_id": bid}))
    assert path["found"] is True and path["length"] == 1


# ── Batch ──

def test_batch_operations(client, db_name):
    ops = {"operations": [
        {"op": "upsert", "data": {"name": "Bulk1", "type": "concept"}},
        {"op": "upsert", "data": {"name": "Bulk2", "type": "concept"}},
    ]}
    d = _data(client.post(f"/api/v1/{db_name}/raw/entities/batch", json=ops))
    assert d["succeeded"] == 2 and d["failed"] == 0


def test_batch_deferred_index_persists_correctly(client, db_name):
    # A batch saves the name index once at the end (P1-10). Verify the index is
    # correct afterward: every created entity dedups on a follow-up upsert.
    n = 60
    ops = {"operations": [{"op": "upsert", "data": {"name": f"B{i}", "type": "concept"}}
                          for i in range(n)]}
    d = _data(client.post(f"/api/v1/{db_name}/raw/entities/batch", json=ops))
    assert d["succeeded"] == n
    assert _data(client.get(f"/api/v1/{db_name}/raw/entities"))["total"] == n
    # follow-up upserts must all resolve to existing entities (index was saved)
    for i in (0, n // 2, n - 1):
        r = _upsert(client, db_name, name=f"B{i}", type="concept")
        assert r["action"] == "updated"


# ── Snapshot export → import round-trip ──

def test_snapshot_export_then_import(client, db_name):
    for i in range(3):
        _upsert(client, db_name, name=f"Snap {i}", type="concept")
    # Export builds <db_root>/AethvionDB.SNAPSHOT and returns it.
    exp = client.get(f"/api/v1/{db_name}/raw/snapshot/download")
    assert exp.status_code == 200
    arr = exp.json()
    assert isinstance(arr, list) and len(arr) == 3

    from aethviondb.config import DATA_DIR
    snap_path = (DATA_DIR / db_name / "AethvionDB.SNAPSHOT").as_posix()
    target = db_name + "_restored"
    rep = client.post("/api/import/run",
                      json={"source_type": "snapshot", "source": snap_path, "db": target})
    assert rep.status_code == 200 and rep.json()["entities"] == 3
    assert _data(client.get(f"/api/v1/{target}/raw/entities"))["total"] == 3


# ── Backups ──

def test_backups_api_roundtrip(client, db_name):
    eid = _upsert(client, db_name, name="Keep")["entity"]["id"]
    meta = _data(client.post(f"/api/v1/{db_name}/backups", json={"label": "snap"}))
    bid = meta["backup_id"]
    assert meta["entity_count"] == 1

    client.delete(f"/api/v1/{db_name}/raw/entities/{eid}?hard=true")
    assert _data(client.get(f"/api/v1/{db_name}/raw/entities"))["total"] == 0

    restored = _data(client.post(f"/api/v1/{db_name}/backups/{bid}/restore"))
    assert restored["restored"] is True
    assert _data(client.get(f"/api/v1/{db_name}/raw/entities"))["total"] == 1

    assert _data(client.delete(f"/api/v1/{db_name}/backups/{bid}"))["deleted"] == bid
    assert _data(client.get(f"/api/v1/{db_name}/backups"))["total"] == 0


# ── SQLite import ──

def test_sqlite_import(client, db_name):
    if not _SAMPLE_DB.exists():
        import pytest
        pytest.skip("sample.db not present")
    rep = client.post("/api/import/run",
                      json={"source_type": "sqlite", "source": _SAMPLE_DB.as_posix(), "db": db_name})
    assert rep.status_code == 200
    body = rep.json()
    assert body["entities"] == 9 and body["relations"] == 7


# ── Capabilities / settings ──

def test_capabilities_and_settings(client):
    caps = _data(client.get("/api/v1/capabilities"))["capabilities"]
    assert any(c["id"] == "embeddings_local" for c in caps)
    # settings round-trip with masking
    client.put("/api/v1/settings", json={"providers": {"openai": {"api_key": "sk-secret-xyz"}}})
    s = _data(client.get("/api/v1/settings"))
    assert s["providers"]["openai"]["set"] is True
    assert "secret" not in str(s)   # raw key never returned


# ── Validation / health (P1-8) ──

def test_validate_clean_database(client, db_name):
    _upsert(client, db_name, name="Solo", type="concept")
    d = _data(client.get(f"/api/v1/{db_name}/raw/validate"))
    assert d["total_entities"] == 1 and d["with_errors"] == 0
    assert d["clean"] == 1 and d["duplicate_groups"] == []


def test_validate_flags_broken_relation(client, db_name):
    # A relation pointing at a non-existent id is a reference-integrity error.
    eid = _upsert(client, db_name, name="Dangling")["entity"]["id"]
    client.patch(f"/api/v1/{db_name}/raw/entities/{eid}", json={"mutations": {
        "sections": {"relations": [{"kind": "depends_on", "target_id": "ws_ghost"}]}}})
    d = _data(client.get(f"/api/v1/{db_name}/raw/validate"))
    assert d["with_errors"] >= 1
    assert any(e["id"] == eid for e in d["entities_with_errors"])


def test_validate_surfaces_soft_deleted(client, db_name):
    eid = _upsert(client, db_name, name="WillDelete")["entity"]["id"]
    client.delete(f"/api/v1/{db_name}/raw/entities/{eid}")   # soft
    d = _data(client.get(f"/api/v1/{db_name}/raw/validate"))
    assert any(x["id"] == eid for x in d["deleted_entities"])


# ── Kinds / ontology enforcement (P2-14) ──

def test_kinds_register_list_delete(client, db_name):
    _ensure = _upsert(client, db_name, name="seed")          # create db
    _data(client.post(f"/api/v1/{db_name}/raw/kinds",
                      json={"name": "software.module", "required_properties": ["language"]}))
    kinds = _data(client.get(f"/api/v1/{db_name}/raw/kinds"))["kinds"]
    assert any(k["name"] == "software.module" and k["required_properties"] == ["language"] for k in kinds)
    _data(client.delete(f"/api/v1/{db_name}/raw/kinds/software.module"))
    assert client.delete(f"/api/v1/{db_name}/raw/kinds/software.module").status_code == 404


def test_init_software_kinds(client, db_name):
    _upsert(client, db_name, name="seed")
    d = _data(client.post(f"/api/v1/{db_name}/raw/kinds/init-software"))
    assert d["added"] >= 1
    names = {k["name"] for k in d["kinds"]}
    assert "software.module" in names and "software.function" in names


def test_kind_required_property_enforced_in_validate(client, db_name):
    # Define a kind that requires 'language', then create an entity missing it.
    _data(client.post(f"/api/v1/{db_name}/raw/kinds",
                      json={"name": "software.module", "required_properties": ["language"]}))
    _upsert(client, db_name, name="NoLang", kind="software.module")   # no language property
    report = _data(client.get(f"/api/v1/{db_name}/raw/validate"))
    checks = {w["check"] for w in report["warning_summary"]}
    assert "kind_schema" in checks
    # satisfying the requirement clears the warning
    _upsert(client, db_name, name="NoLang", kind="software.module",
            properties={"language": "python"})
    report2 = _data(client.get(f"/api/v1/{db_name}/raw/validate"))
    assert "kind_schema" not in {w["check"] for w in report2["warning_summary"]}


# ── Database management (P2-16) ──

def _db_names(client):
    return {d["name"] for d in _data(client.get("/api/v1/"))["databases"]}


def test_create_delete_database(client, db_name):
    _data(client.post("/api/v1/databases", json={"name": db_name}))
    assert db_name in _db_names(client)
    # duplicate create -> 409
    assert client.post("/api/v1/databases", json={"name": db_name}).status_code == 409
    _data(client.delete(f"/api/v1/databases/{db_name}"))
    assert db_name not in _db_names(client)
    # delete missing -> 404
    assert client.delete(f"/api/v1/databases/{db_name}").status_code == 404


def test_rename_database(client, db_name):
    _upsert(client, db_name, name="Keeper")          # creates the db
    new = db_name + "_renamed"
    _data(client.post(f"/api/v1/databases/{db_name}/rename", json={"new_name": new}))
    names = _db_names(client)
    assert new in names and db_name not in names
    assert _data(client.get(f"/api/v1/{new}/raw/entities"))["total"] == 1   # data moved


def test_invalid_db_name_create_rejected(client):
    assert client.post("/api/v1/databases", json={"name": "bad name!"}).status_code == 400


# ── Reindex / warm-up (P1-9) ──

def test_reindex_rebuilds_snapshot_and_index(client, db_name):
    from aethviondb.config import DATA_DIR
    for i in range(4):
        _upsert(client, db_name, name=f"R{i}", type="concept")
    root = DATA_DIR / db_name
    # Delete the derived caches (simulate a cold / corrupted state).
    (root / "AethvionDB.SNAPSHOT").unlink(missing_ok=True)
    (root / "name_index.json").unlink(missing_ok=True)

    d = _data(client.post(f"/api/v1/{db_name}/raw/reindex"))
    assert d["entities"] == 4 and d["index_entries"] == 4
    assert (root / "AethvionDB.SNAPSHOT").exists()
    assert (root / "name_index.json").exists()
    # index rebuilt → dedup works again (upsert finds the existing entity)
    again = _upsert(client, db_name, name="R0", type="concept")
    assert again["action"] == "updated"


# ── API hardening (P0-5) ──

def test_error_envelope_on_404(client, db_name):
    _upsert(client, db_name, name="x")
    r = client.get(f"/api/v1/{db_name}/raw/entities/ws_missing")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found" and body["error"]["message"]


def test_error_envelope_preserves_409_detail(client, db_name):
    eid = _upsert(client, db_name, name="Guard2")["entity"]["id"]
    client.patch(f"/api/v1/{db_name}/raw/entities/{eid}",
                 json={"mutations": {}, "expected_version": 1})
    r = client.patch(f"/api/v1/{db_name}/raw/entities/{eid}",
                     json={"mutations": {}, "expected_version": 1})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "version_conflict"
    assert body["detail"]["error"] == "version_conflict"      # back-compat shape kept


def test_invalid_db_name_rejected(client):
    r = client.get("/api/v1/bad..name/raw/entities")
    assert r.status_code == 400 and r.json()["ok"] is False


def test_validation_error_envelope(client, db_name):
    # upsert requires 'name' — omit it to trigger 422.
    r = client.post(f"/api/v1/{db_name}/raw/entities/upsert", json={"type": "concept"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error" and body["error"]["errors"]


def test_request_body_too_large(client, db_name, monkeypatch):
    monkeypatch.setenv("AETHVIONDB_MAX_BODY_BYTES", "200")
    big = {"name": "Big", "summary": "x" * 1000}
    r = client.post(f"/api/v1/{db_name}/raw/entities/upsert", json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
