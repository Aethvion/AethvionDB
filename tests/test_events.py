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
    monkeypatch.setattr(events, "publish", lambda db, ev: captured.append((db, ev)))
    client.post(f"/api/v1/{db_name}/raw/entities/upsert",
                json={"name": "Watched", "type": "concept"})
    assert any(ev["action"] == "created" and ev["name"] == "Watched"
               for _, ev in captured), captured
