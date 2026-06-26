"""
core/aethviondb/name_index.py
Global name-to-ID lookup for AethvionDB entities.

The index MUST exist and be consulted before any entity file is created.
This prevents duplicate entity files for the same real-world thing.

Storage: data/modes/worldsim/name_index.json
Format:  { "<normalized_name>": "<ws_id>", ... }

Normalization: lowercase, strip outer whitespace, collapse internal whitespace.

Concurrency: a threading.Lock serializes within a process; a cross-process
             FileLock (``<index>.lock``) plus a reload-before-mutate makes the
             dedup gate correct across processes too (a script running alongside
             the server can't miss or clobber registrations).
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from filelock import FileLock

from aethviondb._utils import get_logger, atomic_json_write
from aethviondb.config import AETHVIONDB

logger = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")
_DEFAULT_INDEX_PATH = AETHVIONDB / "default" / "name_index.json"


def _normalize(name: str) -> str:
    """Canonical form for index lookup: lowercase, collapsed whitespace."""
    return _WHITESPACE.sub(" ", name.strip()).lower()


class NameIndex:
    """
    Singleton-like thread-safe name→ID registry.

    Usage
    -----
    idx = NameIndex()
    ws_id = idx.get("Albert Einstein")          # None if not found
    ws_id = idx.get_or_create("Albert Einstein", default_id="ws_abc123")
    idx.register("Albert Einstein", "ws_abc123")
    idx.register_aliases("ws_abc123", ["Einstein", "AE"])
    """

    def __init__(self, index_path: Optional[Path] = None) -> None:
        self._path = index_path or _DEFAULT_INDEX_PATH
        self._lock = threading.Lock()
        # Cross-process lock so concurrent processes (e.g. a script alongside the
        # server) can't clobber each other's index writes or miss each other's
        # registrations — the dedup gate must be correct across processes.
        self._flock = FileLock(str(self._path) + ".lock")
        self._data: dict[str, str] = {}
        self._loaded = False
        self._defer = False   # when True, per-call save/reload are suppressed (batch mode)

    # Loading

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"[NameIndex] Could not load {self._path}: {e} — starting fresh")
                    self._data = {}
            else:
                self._data = {}
            self._loaded = True

    def _save(self) -> None:
        """Atomically write the index to disk. Must be called under _lock."""
        if self._defer:           # batch mode: one save happens on context exit
            return
        atomic_json_write(self._path, self._data, sort_keys=True)

    def _reload_locked(self) -> None:
        """Re-read the index from disk into memory. Must be called under _lock.

        Used inside the cross-process critical section so a mutation sees writes
        another process made since this instance last loaded.
        """
        if self._defer:           # batch mode: don't discard accumulated changes
            return
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass  # keep current in-memory copy on a transient read error
        self._loaded = True

    # Public API

    def get(self, name: str) -> Optional[str]:
        """Return the entity ID for *name*, or None if not indexed."""
        self._ensure_loaded()
        return self._data.get(_normalize(name))

    def get_or_create(self, name: str, default_id: str) -> tuple[str, bool]:
        """
        Return (id, created).
        If *name* is already indexed, returns the existing ID (created=False).
        Otherwise registers *default_id* and returns it (created=True).
        """
        self._ensure_loaded()
        key = _normalize(name)
        with self._flock:               # cross-process: serialize the dedup gate
            with self._lock:
                self._reload_locked()   # see registrations from other processes
                if key in self._data:
                    return self._data[key], False
                self._data[key] = default_id
                self._save()
                return default_id, True

    def register(self, name: str, entity_id: str) -> None:
        """Register a single name→ID mapping. Overwrites silently if already present."""
        self._ensure_loaded()
        key = _normalize(name)
        with self._flock:
            with self._lock:
                self._reload_locked()
                self._data[key] = entity_id
                self._save()
        logger.debug(f"[NameIndex] registered {name!r} → {entity_id}")

    def register_many(self, mapping: dict[str, str]) -> int:
        """Register many name→ID pairs with a single save. Returns the count added.

        Far cheaper than calling register() in a loop (which saves per call) —
        the right path for bulk imports.
        """
        self._ensure_loaded()
        with self._flock:
            with self._lock:
                self._reload_locked()
                for name, entity_id in mapping.items():
                    key = _normalize(name)
                    if key:
                        self._data[key] = entity_id
                self._save()
        return len(mapping)

    def rebuild(self, mapping: dict[str, str]) -> int:
        """Replace the entire index with *mapping* (name→id) in one atomic save.

        Used by reindex/repair to regenerate the index from the entity files,
        dropping any stale entries. Returns the resulting entry count.
        """
        self._ensure_loaded()
        with self._flock:
            with self._lock:
                self._data = {}
                for name, entity_id in mapping.items():
                    key = _normalize(name)
                    if key and entity_id:
                        self._data[key] = entity_id
                self._save()
                return len(self._data)

    @contextmanager
    def deferred_save(self):
        """Suppress per-call disk saves for a batch, writing the index once at the end.

        Turns O(n) full-file index writes into one. Does **not** hold the
        cross-process lock across the batch — that would invert lock order with
        the entity write path (which takes the write lock then the index lock)
        and could deadlock. Instead it syncs once up front, accumulates changes
        in memory, and writes once on exit. Use on a per-request NameIndex
        instance (the API builds one per request).
        """
        with self._flock:
            with self._lock:
                self._reload_locked()     # sync once up front (defer not set yet)
        self._defer = True
        try:
            yield
        finally:
            self._defer = False
            with self._flock:
                with self._lock:
                    self._save()          # single write for the whole batch

    def register_aliases(self, entity_id: str, aliases: list[str]) -> None:
        """Register multiple alias names for the same entity ID."""
        self._ensure_loaded()
        with self._flock:
            with self._lock:
                self._reload_locked()
                for alias in aliases:
                    key = _normalize(alias)
                    if key and key not in self._data:
                        self._data[key] = entity_id
                self._save()

    def unregister(self, name: str) -> bool:
        """Remove a name from the index. Returns True if it was present."""
        self._ensure_loaded()
        key = _normalize(name)
        with self._flock:
            with self._lock:
                self._reload_locked()
                if key in self._data:
                    del self._data[key]
                    self._save()
                    return True
                return False

    def list_all(self) -> dict[str, str]:
        """Return a snapshot of the full index (name → id)."""
        self._ensure_loaded()
        with self._lock:
            return dict(self._data)

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._data)

    def reload(self) -> None:
        """Force re-read from disk (e.g. after external mutation)."""
        with self._lock:
            self._loaded = False
        self._ensure_loaded()


# Module-level singleton — import and share this across the package
_default_index: Optional[NameIndex] = None


def get_index() -> NameIndex:
    """Return the shared module-level NameIndex instance."""
    global _default_index
    if _default_index is None:
        _default_index = NameIndex()
    return _default_index
