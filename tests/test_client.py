"""
tests/test_client.py
The dependency-free AethvionClient (P2-12), tested end-to-end against a real
server running in a background thread.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from aethviondb import AethvionClient, AethvionError

pytest.importorskip("uvicorn")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server():
    import uvicorn
    from aethviondb.server import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_crud_and_search(live_server, db_name):
    c = AethvionClient(base_url=live_server, db=db_name, actor="tester")
    e = c.upsert("Ada Lovelace", type="person", summary="Pioneer.")
    assert e["name"] == "Ada Lovelace" and e["id"]

    assert c.get(e["id"])["name"] == "Ada Lovelace"
    assert c.get("ws_missing") is None

    e2 = c.update(e["id"], {"sections": {"core": {"summary": "Updated."}}})
    assert e2["version"] == e["version"] + 1

    results = c.search("ada")
    assert any(r["name"] == "Ada Lovelace" for r in results)

    assert len(c.entities()) == 1
    c.delete(e["id"])
    assert c.entities() == []           # soft-deleted, gone from active


def test_graph_and_path(live_server, db_name):
    c = AethvionClient(base_url=live_server, db=db_name)
    a = c.upsert("A", relations=[{"kind": "related_to", "target_name": "B"}])
    bid = next(r["id"] for r in c.entities() if r["name"] == "B")
    assert c.traverse(a["id"])["node_count"] >= 2
    assert c.path(a["id"], bid)["found"] is True


def test_optimistic_concurrency_raises(live_server, db_name):
    c = AethvionClient(base_url=live_server, db=db_name)
    e = c.upsert("Guarded")
    c.update(e["id"], {"sections": {"core": {"summary": "v2"}}}, expected_version=1)
    with pytest.raises(AethvionError) as ei:
        c.update(e["id"], {"sections": {"core": {"summary": "boom"}}}, expected_version=1)
    assert ei.value.status == 409


def test_watch_receives_event(live_server, db_name):
    c = AethvionClient(base_url=live_server, db=db_name, actor="watcher")
    received: list = []

    def _watch():
        for ev in c.watch():
            received.append(ev)
            break
    th = threading.Thread(target=_watch, daemon=True)
    th.start()
    time.sleep(0.7)                     # let the watcher connect
    c.upsert("LiveOne", type="concept")
    th.join(timeout=6)
    assert received, "no event received from watch()"
    assert received[0]["name"] == "LiveOne" and received[0]["actor"] == "watcher"
