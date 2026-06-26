"""
tests/test_events.py
Live change feed (P0-4): the in-process pub/sub bus, and that API writes emit
change events.
"""
from __future__ import annotations

import asyncio

import aethviondb.events as events


class TestEventBus:
    def test_publish_reaches_subscriber(self):
        async def go():
            q, unsub = events.subscribe("dbA")
            events.publish("dbA", {"action": "created", "id": "ws_1"})
            ev = await asyncio.wait_for(q.get(), 1.0)
            unsub()
            return ev
        ev = asyncio.run(go())
        assert ev["action"] == "created" and ev["id"] == "ws_1"

    def test_publish_isolated_per_db(self):
        async def go():
            qa, ua = events.subscribe("dbX")
            qb, ub = events.subscribe("dbY")
            events.publish("dbX", {"n": 1})
            got = await asyncio.wait_for(qa.get(), 1.0)
            empty = qb.empty()
            ua(); ub()
            return got, empty
        got, empty = asyncio.run(go())
        assert got["n"] == 1 and empty is True

    def test_unsubscribe_stops_delivery(self):
        async def go():
            q, unsub = events.subscribe("dbZ")
            unsub()
            events.publish("dbZ", {"n": 1})        # no subscribers now
            return events.subscriber_count("dbZ")
        assert asyncio.run(go()) == 0

    def test_overflow_drops_oldest_not_raises(self):
        async def go():
            q, unsub = events.subscribe("dbO")
            # Fill well past the queue cap; publish must never raise or block.
            for i in range(events._MAX_QUEUE + 50):
                events.publish("dbO", {"i": i})
            size = q.qsize()
            unsub()
            return size
        size = asyncio.run(go())
        assert size <= events._MAX_QUEUE      # bounded; oldest dropped


def test_api_write_emits_event(client, db_name, monkeypatch):
    captured: list = []
    monkeypatch.setattr(events, "publish", lambda db, ev, **k: captured.append((db, ev)))
    client.post(f"/api/v1/{db_name}/raw/entities/upsert",
                json={"name": "Watched", "type": "concept"})
    assert any(ev["action"] == "created" and ev["name"] == "Watched"
               for _, ev in captured), captured


class TestReplayAndThreadsafe:
    def test_recorded_events_get_ids_and_backlog(self):
        async def go():
            q, unsub = events.subscribe("repl")
            events.publish("repl", {"action": "created", "name": "A"})
            ev = await asyncio.wait_for(q.get(), 1.0)
            unsub()
            return ev
        ev = asyncio.run(go())
        assert isinstance(ev["_seq"], int) and ev["name"] == "A"   # entity payload intact
        # backlog after an earlier seq returns this event
        later = events.backlog("repl", ev["_seq"] - 1)
        assert any(e["_seq"] == ev["_seq"] for e in later)
        # nothing after the latest seq
        assert events.backlog("repl", ev["_seq"]) == []

    def test_publish_threadsafe_records_without_loop(self):
        events.set_loop(None)
        before = events.backlog("tsdb", 0)
        events.publish_threadsafe("tsdb", {"action": "import", "name": "x"})
        after = events.backlog("tsdb", 0)
        assert len(after) == len(before) + 1   # recorded even with no loop to fan out to

    def test_presence_events_are_not_buffered(self):
        n0 = len(events.backlog("presdb", 0))
        events.publish("presdb", {"action": "presence", "count": 3}, record=False)
        assert len(events.backlog("presdb", 0)) == n0   # ephemeral — not replayed


def test_import_emits_event(client, db_name, monkeypatch):
    from pathlib import Path
    sample = Path(__file__).resolve().parent.parent / "benchmark_databases" / "sample.db"
    if not sample.exists():
        import pytest
        pytest.skip("sample.db not present")
    captured: list = []
    monkeypatch.setattr(events, "publish", lambda db, ev, **k: captured.append((db, ev)))
    client.post("/api/import/run",
                json={"source_type": "sqlite", "source": sample.as_posix(), "db": db_name})
    assert any(ev["action"] == "import" for _, ev in captured), captured
