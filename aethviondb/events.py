"""
aethviondb/events.py
In-process realtime change feed — the primitive that lets agents work *live*.

Write handlers ``publish`` a small change event (created / updated / deleted);
SSE clients ``subscribe`` and receive them as they happen. This is the seam that
turns the store into a shared, live brain: one agent's write is immediately
visible to every other agent and to the dashboard.

Robustness (P1-11):
  * Every recorded event gets a monotonic ``id``; a per-database ring buffer
    keeps the recent ones so a client that briefly disconnects can replay what it
    missed (the SSE endpoint honours the ``Last-Event-ID`` header).
  * ``publish_threadsafe`` lets worker threads (import, vectorize) emit safely by
    scheduling the fan-out on the server event loop.
  * ``presence`` events (ephemeral, not buffered) carry the live subscriber count.

Scope: in-process, single worker. This module is where a cross-process broker
(Redis, NATS, …) slots in for multi-host multiplayer without changing call sites.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Optional

# db name → set of subscriber queues
_subscribers: dict[str, set[asyncio.Queue]] = {}
# db name → ring buffer of recent recorded events (for reconnect replay)
_buffers: dict[str, deque] = {}

_seq = 0
_lock = threading.Lock()          # guards _seq and _buffers
_loop: Optional[asyncio.AbstractEventLoop] = None

_MAX_QUEUE = 2000     # per-subscriber backlog cap (slow client drops oldest)
_BUFFER    = 500      # recent events kept per db for replay


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the server event loop so worker threads can fan out onto it."""
    global _loop
    _loop = loop


def _record(db: str, event: dict) -> dict:
    """Stamp an event with a monotonic stream sequence (``_seq``) and append it to
    the db's ring buffer. Kept separate from the payload's ``id`` (the entity id)
    so it can drive the SSE ``id:`` field / Last-Event-ID without clobbering it."""
    global _seq
    with _lock:
        _seq += 1
        event = {**event, "_seq": _seq}
        _buffers.setdefault(db, deque(maxlen=_BUFFER)).append(event)
        return event


def _fanout(db: str, event: dict) -> None:
    """Deliver to every subscriber queue. Must run on the event loop."""
    for q in list(_subscribers.get(db, ())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(event)
            except Exception:
                pass


def publish(db: str, event: dict, record: bool = True) -> None:
    """Publish an event. Call from the event loop. ``record=False`` for ephemeral
    events (e.g. presence) that shouldn't enter the replay buffer."""
    ev = _record(db, event) if record else event
    _fanout(db, ev)


def publish_threadsafe(db: str, event: dict) -> None:
    """Publish from a worker thread: record now, fan out on the server loop."""
    ev = _record(db, event)
    loop = _loop
    if loop is not None:
        try:
            loop.call_soon_threadsafe(_fanout, db, ev)
        except RuntimeError:
            pass   # loop closed — event is still in the replay buffer


def backlog(db: str, after_seq: int) -> list[dict]:
    """Recent recorded events with stream seq greater than *after_seq* (replay)."""
    with _lock:
        return [e for e in _buffers.get(db, ()) if e.get("_seq", 0) > after_seq]


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
