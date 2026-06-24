"""
aethviondb/events.py
In-process realtime change feed — the primitive that lets agents work *live*.

Write handlers ``publish`` a small change event (created / updated / deleted);
SSE clients ``subscribe`` and receive them as they happen. This is the seam that
turns the store into a shared, live brain: one agent's write is immediately
visible to every other agent and to the dashboard.

Scope: in-process, single worker. Events are fanned out to per-subscriber
asyncio queues. This is deliberately the simplest thing that works for a local
host; when the multiplayer/edge story arrives, this module is where a
cross-process broker (Redis, NATS, …) slots in without changing call sites.
"""
from __future__ import annotations

import asyncio
from typing import Optional

# db name → set of subscriber queues
_subscribers: dict[str, set[asyncio.Queue]] = {}

# Cap a subscriber's backlog; a slow client drops its oldest events rather than
# growing memory without bound or blocking publishers.
_MAX_QUEUE = 2000


def publish(db: str, event: dict) -> None:
    """Fan an event out to every subscriber of *db*. Non-blocking; never raises.

    Safe to call from an async request handler (same event loop as the queues).
    """
    subs = _subscribers.get(db)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event to make room for the newest.
            try:
                q.get_nowait()
                q.put_nowait(event)
            except Exception:
                pass


def subscribe(db: str) -> tuple[asyncio.Queue, "callable"]:
    """Register a subscriber for *db*. Returns (queue, unsubscribe).

    Must be called from within a running event loop (the SSE handler).
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    _subscribers.setdefault(db, set()).add(q)

    def _unsubscribe() -> None:
        s = _subscribers.get(db)
        if s is not None:
            s.discard(q)
            if not s:
                _subscribers.pop(db, None)

    return q, _unsubscribe


def subscriber_count(db: Optional[str] = None) -> int:
    """Number of live subscribers — for one db, or across all when db is None."""
    if db is not None:
        return len(_subscribers.get(db, ()))
    return sum(len(s) for s in _subscribers.values())
