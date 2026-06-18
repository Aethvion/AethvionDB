"""
core/aethviondb/snapshot.py
AethvionDB entity cache — in-memory + on-disk, with O(1) freshness.

Why
---
``EntityWriter.list_all()`` would otherwise open and parse N individual
``ws_*.json`` files.  For large databases (30 000+ entities) that is seconds
of file-syscall latency on Windows (NTFS + Defender × N).

How it works now (three layers)
-------------------------------
1. **In-memory cache** (``_MEM``) — the authoritative, hot copy.  Parsed once,
   then served from RAM.  Reads are dict lookups, not file I/O.

2. **Incremental writes** — every entity write patches the in-memory cache in
   place (O(1)) and bumps a **generation counter**.  Writes never trigger a
   full N-file rebuild; the cache simply absorbs the one changed entity.

3. **On-disk snapshot** — a warm-start cache so a fresh process (or DB switch)
   can repopulate RAM from one file instead of N.  Three files per database:

     <db_root>/AethvionDB.SNAPSHOT            — compact JSON array, full entities
     <db_root>/AethvionDB.SNAPSHOT.meta.json  — {entity_count, built_gen, ...}
     <db_root>/AethvionDB.GEN                 — {"gen": N}, current data version

Generation counter (O(1) freshness)
------------------------------------
Every mutation bumps ``AethvionDB.GEN``.  The snapshot records the generation
it was built at (``built_gen``).  Freshness is a single small file read and an
integer compare — never a directory scan or per-file ``stat()``.  This is how
AethvionDB tracks validity at scale; it does not depend on filesystem mtimes.

Lightweight list-index (C)
--------------------------
``get_lite()`` returns only the columns the list view renders (id, name, type,
kind, status, summary, tags, counts, dates), derived from the in-memory cache
and itself cached.  The explorer is served this projection — a small fraction
of the full payload per page — while full entity bodies load on demand when a
row is opened.  Not all information has to be available at once.

Layout note
-----------
In Aethvion Suite entities live in ``<db_root>/entities/``, so snapshot files
land in ``<db_root>/`` (``entities_dir.parent``).  Callers pass both the
``db_root`` (where snapshot files live) and the ``entities_dir`` (the raw
``ws_*.json`` source) so the engine can rebuild when the warm cache is absent.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from aethviondb._utils import get_logger

logger = get_logger(__name__)

# ── File name constants ───────────────────────────────────────────────────────

SNAPSHOT_FILE = "AethvionDB.SNAPSHOT"
META_FILE     = "AethvionDB.SNAPSHOT.meta.json"
GEN_FILE      = "AethvionDB.GEN"

# Cap a lite summary so the list-view payload stays small regardless of body size.
_SUMMARY_CAP      = 300
# Persist the in-memory cache to disk at most this often (seconds) while writes
# stream in — keeps the warm-start snapshot reasonably current without a disk
# rewrite per write.
_FLUSH_INTERVAL_S = 15.0
# How many databases keep a full in-memory cache simultaneously.  The active DB
# is what matters; older ones are flushed and dropped to bound memory.
_MAX_FULL_CACHES  = 2


# ── Path helpers ──────────────────────────────────────────────────────────────

def snapshot_path(db_root: Path) -> Path:
    return db_root / SNAPSHOT_FILE


def meta_path(db_root: Path) -> Path:
    return db_root / META_FILE


def gen_path(db_root: Path) -> Path:
    return db_root / GEN_FILE


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* via temp-file → replace (never a partial file)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Generation counter (O(1) freshness) ───────────────────────────────────────

_gen_locks: dict[str, threading.Lock] = {}
_gen_guard = threading.Lock()


def _gen_lock(db_root: Path) -> threading.Lock:
    key = str(db_root)
    with _gen_guard:
        lk = _gen_locks.get(key)
        if lk is None:
            lk = _gen_locks[key] = threading.Lock()
        return lk


def read_gen(db_root: Path) -> int:
    """Return the current data generation for *db_root* (0 if never written)."""
    try:
        return int(_read_json(gen_path(db_root)).get("gen", 0))
    except Exception:
        return 0


def bump_gen(db_root: Path) -> int:
    """Increment and persist the generation counter.  Returns the new value."""
    with _gen_lock(db_root):
        nxt = read_gen(db_root) + 1
        try:
            db_root.mkdir(parents=True, exist_ok=True)
            _atomic_write(gen_path(db_root), json.dumps({"gen": nxt}))
        except Exception as exc:
            logger.debug(f"[Snapshot] gen bump failed: {exc}")
        return nxt


# ── Lightweight projection ────────────────────────────────────────────────────

def _to_lite(e: dict[str, Any]) -> dict[str, Any]:
    """Project a full entity down to the columns the list view renders."""
    sec  = e.get("sections") or {}
    core = sec.get("core") or {}
    summary = core.get("summary") or ""
    if len(summary) > _SUMMARY_CAP:
        summary = summary[:_SUMMARY_CAP] + "…"
    return {
        "id":              e.get("id"),
        "name":            e.get("name"),
        "type":            e.get("type"),
        "kind":            e.get("kind"),
        "status":          e.get("status"),
        "version":         e.get("version"),
        "created":         e.get("created"),
        "updated":         e.get("updated"),
        "source":          e.get("source"),
        "summary":         summary,
        "tags":            list(core.get("tags") or []),
        "relations_count": len(sec.get("relations") or []),
        "stubs_count":     len(sec.get("stubs") or []),
    }


# ── In-memory cache ───────────────────────────────────────────────────────────

@dataclass
class _Mem:
    gen:        int
    by_id:      dict[str, dict]
    dirty:      bool                       = False
    last_flush: float                      = 0.0
    lite:       Optional[list[dict]]       = None   # derived; rebuilt lazily


_MEM:  "dict[str, _Mem]"          = {}
_LOCK = threading.RLock()

_load_locks: dict[str, threading.Lock] = {}
_load_guard = threading.Lock()


def _load_lock(db_root: Path) -> threading.Lock:
    key = str(db_root)
    with _load_guard:
        lk = _load_locks.get(key)
        if lk is None:
            lk = _load_locks[key] = threading.Lock()
        return lk


def _persist(db_root: Path, entities: list[dict], gen: int) -> None:
    """Write the snapshot + meta to disk atomically, stamped at *gen*."""
    t0 = time.perf_counter()
    try:
        db_root.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            snapshot_path(db_root),
            json.dumps(entities, ensure_ascii=False, separators=(",", ":")),
        )
        _atomic_write(meta_path(db_root), json.dumps({
            "v":            2,
            "built_at":     _now_iso(),
            "entity_count": len(entities),
            "built_gen":    gen,
            "elapsed_ms":   round((time.perf_counter() - t0) * 1000, 1),
        }, indent=2, ensure_ascii=False))
    except Exception as exc:
        logger.warning(f"[Snapshot] persist failed: {exc}")


def _load_disk_if_fresh(db_root: Path, entities_dir: Path) -> Optional[list[dict]]:
    """Return entities from the on-disk snapshot if it can be trusted, else None.

    Trusted when the snapshot's ``built_gen`` matches the current generation.
    Legacy snapshots (no ``built_gen``) are adopted only when their entity count
    matches the actual ``ws_*.json`` count — a single directory enumeration,
    never N per-file ``stat()`` calls.
    """
    snap = snapshot_path(db_root)
    if not snap.exists():
        return None

    meta = None
    if meta_path(db_root).exists():
        try:
            meta = _read_json(meta_path(db_root))
        except Exception:
            meta = None

    cur_gen   = read_gen(db_root)
    built_gen = meta.get("built_gen") if isinstance(meta, dict) else None

    if built_gen is not None:
        if built_gen == cur_gen:
            try:
                return _read_json(snap)
            except Exception:
                return None
        return None  # stale — a write happened since the snapshot was built

    # Legacy snapshot without a generation stamp: adopt only if the count still
    # matches what's on disk, then re-persist so it's fully fast next time.
    try:
        data = _read_json(snap)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    try:
        actual = sum(1 for _ in entities_dir.glob("ws_*.json"))
    except OSError:
        return None
    if len(data) == actual:
        _persist(db_root, data, cur_gen)
        logger.info(f"[Snapshot] Adopted legacy snapshot ({len(data)} entities) for {db_root.name}")
        return data
    return None


def _evict_locked() -> None:
    """Drop least-recently-used full caches beyond the cap (flush if dirty).

    Must be called while holding ``_LOCK``.
    """
    while len(_MEM) > _MAX_FULL_CACHES:
        old_key = next(iter(_MEM))            # oldest insertion = LRU
        old     = _MEM.pop(old_key)
        if old.dirty:
            try:
                _persist(Path(old_key), list(old.by_id.values()), old.gen)
            except Exception:
                pass


def _ensure_loaded(
    db_root:      Path,
    entities_dir: Path,
    raw_loader:   Callable[[], list[dict]],
) -> _Mem:
    """Return the in-memory cache for *db_root*, loading it if cold/stale."""
    key = str(db_root)

    # Fast path — warm cache whose generation still matches disk.
    with _LOCK:
        mem = _MEM.get(key)
        if mem is not None and mem.gen == read_gen(db_root):
            _MEM.pop(key); _MEM[key] = mem    # mark most-recently-used
            return mem

    # Slow path — serialize cold loads per DB so we never double-parse 36 MB.
    with _load_lock(db_root):
        with _LOCK:
            mem = _MEM.get(key)
            if mem is not None and mem.gen == read_gen(db_root):
                return mem

        gen_before = read_gen(db_root)
        entities   = _load_disk_if_fresh(db_root, entities_dir)
        source     = "snapshot"
        if entities is None:
            entities = raw_loader()           # the slow O(N-files) path
            source   = "files"
            _persist(db_root, entities, gen_before)

        with _LOCK:
            mem = _Mem(
                gen=gen_before,
                by_id={e["id"]: e for e in entities if e.get("id")},
                last_flush=time.monotonic(),
            )
            _MEM[key] = mem
            _evict_locked()
        logger.debug(f"[Snapshot] Loaded {len(entities)} entities from {source} for {db_root.name}")
        return mem


def _maybe_flush(db_root: Path, mem: _Mem) -> None:
    """Persist the cache to disk if it's dirty and the flush interval elapsed."""
    with _LOCK:
        if not mem.dirty or (time.monotonic() - mem.last_flush) < _FLUSH_INTERVAL_S:
            return
        entities       = list(mem.by_id.values())
        gen            = mem.gen
        mem.dirty      = False
        mem.last_flush = time.monotonic()
    _persist(db_root, entities, gen)


# ── Public read API ───────────────────────────────────────────────────────────

def get_all(
    db_root:         Path,
    entities_dir:    Path,
    raw_loader:      Callable[[], list[dict]],
    include_deleted: bool = False,
) -> list[dict]:
    """Return full entities, served from the in-memory cache."""
    mem = _ensure_loaded(db_root, entities_dir, raw_loader)
    _maybe_flush(db_root, mem)
    with _LOCK:
        vals = list(mem.by_id.values())
    if include_deleted:
        return vals
    return [e for e in vals if e.get("status") != "deleted"]


def get_lite(
    db_root:         Path,
    entities_dir:    Path,
    raw_loader:      Callable[[], list[dict]],
    include_deleted: bool = False,
) -> list[dict]:
    """Return the lightweight list-view projection from the in-memory cache."""
    mem = _ensure_loaded(db_root, entities_dir, raw_loader)
    _maybe_flush(db_root, mem)
    with _LOCK:
        if mem.lite is None:
            mem.lite = [_to_lite(e) for e in mem.by_id.values()]
        lite = mem.lite
        if include_deleted:
            return list(lite)
        return [e for e in lite if e.get("status") != "deleted"]


# ── Public write API (called by EntityWriter) ─────────────────────────────────

def put(db_root: Path, entity: dict[str, Any]) -> None:
    """Patch one entity into the cache and bump the generation (O(1) write)."""
    g   = bump_gen(db_root)
    eid = entity.get("id")
    if not eid:
        return
    with _LOCK:
        mem = _MEM.get(str(db_root))
        if mem is not None:
            mem.by_id[eid] = entity
            mem.gen        = g
            mem.lite       = None
            mem.dirty      = True


def remove(db_root: Path, entity_id: str) -> None:
    """Drop a hard-deleted entity from the cache and bump the generation."""
    g = bump_gen(db_root)
    with _LOCK:
        mem = _MEM.get(str(db_root))
        if mem is not None:
            mem.by_id.pop(entity_id, None)
            mem.gen   = g
            mem.lite  = None
            mem.dirty = True


# ── Maintenance ───────────────────────────────────────────────────────────────

def flush(db_root: Path) -> None:
    """Force-persist the cache to disk if dirty (e.g. on shutdown)."""
    with _LOCK:
        mem = _MEM.get(str(db_root))
        if mem is None or not mem.dirty:
            return
        entities       = list(mem.by_id.values())
        gen            = mem.gen
        mem.dirty      = False
        mem.last_flush = time.monotonic()
    _persist(db_root, entities, gen)


def invalidate(db_root: Path) -> None:
    """Drop the cache and delete snapshot files (for out-of-band file changes).

    Call this after operations that replace entity files wholesale outside the
    normal write path — e.g. restoring a backup.
    """
    with _LOCK:
        _MEM.pop(str(db_root), None)
    for p in (snapshot_path(db_root), meta_path(db_root)):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    bump_gen(db_root)
    logger.debug(f"[Snapshot] Invalidated {db_root.name}")


# ── Backward-compatible shims ─────────────────────────────────────────────────

def is_fresh(db_root: Path, entities_dir: Path | None = None) -> bool:
    """True if the on-disk snapshot is current (generation match).  O(1)."""
    if not snapshot_path(db_root).exists() or not meta_path(db_root).exists():
        return False
    try:
        meta = _read_json(meta_path(db_root))
    except Exception:
        return False
    return meta.get("built_gen") == read_gen(db_root)


def build(db_root: Path, entities: list[dict[str, Any]]) -> None:
    """Persist *entities* to the on-disk snapshot, stamped at the current gen."""
    _persist(db_root, entities, read_gen(db_root))


def load(db_root: Path) -> list[dict[str, Any]]:
    """Read entities directly from the on-disk snapshot file (no caching)."""
    snap = snapshot_path(db_root)
    if not snap.exists():
        return []
    try:
        return _read_json(snap)
    except Exception as exc:
        logger.warning(f"[Snapshot] Read failed: {exc}")
        return []
