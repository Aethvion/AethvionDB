"""
tests/test_baked.py
The baked-snapshot subsystem (P-A.3) — list / trigger / get / entities / search /
download / rename / delete, end-to-end through the API.
"""
from __future__ import annotations

import time


def _data(resp):
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True, body
    return body["data"]


def _seed(client, db, n=4):
    for i in range(n):
        client.post(f"/api/v1/{db}/raw/entities/upsert",
                    json={"name": f"Doc {i}", "type": "concept", "summary": f"summary {i}",
                          "tags": ["bench"]})


def _wait_done(client, db, name, tries=60):
    for _ in range(tries):
        r = client.get(f"/api/v1/{db}/baked/{name}")
        if r.status_code == 200 and r.json()["data"].get("status") == "done":
            return r.json()["data"]
        time.sleep(0.1)
    raise AssertionError(f"bake {name!r} did not complete: {r.text}")


def test_bake_lifecycle_jsonl(client, db_name):
    _seed(client, db_name, 4)
    started = _data(client.post(f"/api/v1/{db_name}/baked",
                                json={"name": "snap", "format": "jsonl"}))
    assert started["started"] is True
    meta = _wait_done(client, db_name, "snap")
    assert meta["entity_count"] == 4 and meta["format"] == "jsonl"

    # appears in the list
    listing = _data(client.get(f"/api/v1/{db_name}/baked"))
    assert any(b["name"] == "snap" for b in listing["bakes"])

    # entities + search within the snapshot
    ents = _data(client.get(f"/api/v1/{db_name}/baked/snap/entities"))
    assert ents["total"] == 4
    found = _data(client.post(f"/api/v1/{db_name}/baked/snap/search", json={"query": "Doc 1"}))
    assert any("Doc 1" == r["name"] for r in found["results"])

    # download returns the file
    dl = client.get(f"/api/v1/{db_name}/baked/snap/download")
    assert dl.status_code == 200 and len(dl.content) > 0


def test_bake_rename_and_delete(client, db_name):
    _seed(client, db_name, 2)
    _data(client.post(f"/api/v1/{db_name}/baked", json={"name": "snap", "format": "jsonl"}))
    _wait_done(client, db_name, "snap")

    _data(client.patch(f"/api/v1/{db_name}/baked/snap", json={"new_name": "snap2"}))
    assert client.get(f"/api/v1/{db_name}/baked/snap").status_code == 404
    assert _wait_done(client, db_name, "snap2")["entity_count"] == 2

    _data(client.delete(f"/api/v1/{db_name}/baked/snap2"))
    assert client.get(f"/api/v1/{db_name}/baked/snap2").status_code == 404


def test_bake_get_missing_404(client, db_name):
    _seed(client, db_name, 1)
    assert client.get(f"/api/v1/{db_name}/baked/nope").status_code == 404
