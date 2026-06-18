"""
core/aethviondb/folder_distiller.py
Distil an entire folder of documents into an AethvionDB database.

Persistence model
-----------------
Two sidecar files live in the database root:

  AethvionDB.DISTILLINFO  — JSON job metadata + progress (always small)
  AethvionDB.DISTILLQUEUE — newline-separated relative file paths (can be large)

This lets the job survive a pause, an application restart, or a crash:
on resume the engine re-reads both files and continues from `next_index`.

Pause mechanism
---------------
Each db root has an asyncio.Event in ``_pause_events``.  The background
task checks the event before every file; clearing the event signals "please
pause after the current file".  The event is always created from the async
context (route handler) so it is bound to the running event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from aethviondb._utils import get_logger

logger = get_logger(__name__)

# Constants

_INFO_FILE  = "AethvionDB.DISTILLINFO"
_QUEUE_FILE = "AethvionDB.DISTILLQUEUE"

#: Extensions treated as readable text and eligible for distillation.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    # Prose / docs
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex",
    # Web
    ".html", ".htm",
    # Data / config
    ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml",
    # Code
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs",
    ".java", ".cpp", ".c", ".h", ".hpp",
    ".rb", ".go", ".rs", ".php", ".cs", ".swift", ".kt",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".r", ".lua", ".log",
})

# In-process job state

_active_tasks: dict[str, asyncio.Task] = {}  # str(db_root) → Task
_pause_events: dict[str, asyncio.Event] = {}  # str(db_root) → Event (set=running)


# Helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_size(b: int) -> str:
    if b < 1024:       return f"{b} B"
    if b < 1024 ** 2:  return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:  return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def _content_hash(text: str) -> str:
    """Return a SHA-256 hash of the UTF-8 encoded text as 'sha256:<hex>'."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# DISTILLINFO I/O

def read_distill_info(db_root: Path) -> dict:
    """Return contents of AethvionDB.DISTILLINFO, or {} if absent / unreadable."""
    p = db_root / _INFO_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_distill_info(db_root: Path, data: dict) -> None:
    """Write data to AethvionDB.DISTILLINFO (best-effort, never raises)."""
    try:
        (db_root / _INFO_FILE).write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"[FolderDistiller] Could not write {_INFO_FILE}: {exc}")


def _read_queue(db_root: Path) -> list[str]:
    p = db_root / _QUEUE_FILE
    if p.exists():
        try:
            return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except Exception:
            return []
    return []


def _write_queue(db_root: Path, paths: list[str]) -> None:
    try:
        (db_root / _QUEUE_FILE).write_text("\n".join(paths), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[FolderDistiller] Could not write {_QUEUE_FILE}: {exc}")


def _save_progress(
    db_root:      Path,
    base_info:    dict,
    status:       str,
    next_index:   int,
    processed:    int,
    failed:       int,
    skipped:      int,
    failed_list:  list,
    log:          list | None = None,
    current_file: str = "",
) -> None:
    write_distill_info(db_root, {
        **base_info,
        "status":        status,
        "next_index":    next_index,
        "processed":     processed,
        "failed":        failed,
        "skipped":       skipped,
        "failed_list":   failed_list[-50:],   # cap to last 50 failures
        "log":           (log or [])[-100:],  # cap to last 100 log lines
        "current_file":  current_file,
        "last_updated":  _now_iso(),
    })


# Public query helpers

def is_running(db_root: Path) -> bool:
    return str(db_root) in _active_tasks


# Scan (sync — call via asyncio.to_thread)

def scan_folder(folder_path: str) -> dict:
    """
    Walk the folder and return metadata without reading any file contents.
    Safe to call via asyncio.to_thread for large directories.
    """
    root = Path(folder_path)
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder_path}")

    total_files     = 0
    supported_files = 0
    total_size      = 0
    ext_counts: dict[str, int] = {}
    ext_sizes:  dict[str, int] = {}

    for dirpath, dirs, filenames in os.walk(root):
        dirs.sort()
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            ext = fp.suffix.lower()
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            total_files             += 1
            total_size              += size
            ext_counts[ext]          = ext_counts.get(ext, 0) + 1
            ext_sizes[ext]           = ext_sizes.get(ext, 0) + size
            if ext in SUPPORTED_EXTENSIONS:
                supported_files += 1

    top_types = sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:12]

    return {
        "folder_path":      folder_path,
        "total_files":      total_files,
        "supported_files":  supported_files,
        "total_size_bytes": total_size,
        "total_size_fmt":   _fmt_size(total_size),
        "top_types": [
            {
                "ext":        e if e else "(no ext)",
                "count":      c,
                "size_bytes": ext_sizes.get(e, 0),
                "supported":  e in SUPPORTED_EXTENSIONS,
            }
            for e, c in top_types
        ],
    }


# Job preparation (sync — call via asyncio.to_thread)

def prepare_start_job(
    db_root:     Path,
    folder_path: str,
    model:       str,
    source:      str,
    concurrency: int = 1,
) -> int:
    """
    Scan the folder, write the queue file and an initial DISTILLINFO.
    Returns total file count.  Runs in a thread (may take seconds for
    very large directories).
    """
    root = Path(folder_path)
    file_list: list[str] = []
    for dirpath, dirs, filenames in os.walk(root):
        dirs.sort()
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            try:
                rel = str(fp.relative_to(root))
            except ValueError:
                rel = str(fp)
            file_list.append(rel)

    _write_queue(db_root, file_list)
    total = len(file_list)

    now = _now_iso()
    write_distill_info(db_root, {
        "folder_path":  folder_path,
        "started_at":   now,
        "last_updated": now,
        "status":       "starting",
        "model":        model,
        "source":       source,
        "concurrency":  max(1, concurrency),
        "total_files":  total,
        "next_index":   0,
        "processed":    0,
        "failed":       0,
        "skipped":      0,
        "failed_list":  [],
        "log":          [],
        "current_file": "",
    })
    return total


# Per-file distillation helper

async def _distill_one_file(
    distiller,
    source:      str,
    fp:          Path,
    rel:         str,
    index:       int,
    folder_root: Path | None = None,
) -> dict:
    """
    Read and distil a single file.  Always returns a result dict — never raises.

    Keys: status ('ok'|'fail'|'skip'), entity_id, entity_name, file_hash,
          size, rel_path, fname, rel, index.
    """
    fname = fp.name
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")

        # Binary / empty heuristics
        if not content.strip():
            return {"status": "skip", "fname": fname, "rel": rel, "index": index}
        if len(content) > 0 and content.count("�") / len(content) > 0.15:
            return {"status": "skip", "fname": fname, "rel": rel, "index": index}

        file_hash = _content_hash(content)
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0

        result = await distiller.distill(
            content=content,
            source=source,
            source_path=rel,
            source_hash=file_hash,
            source_size=size,
        )
        if result["errors"]:
            return {
                "status": "fail",
                "error":  result["errors"][0],
                "fname":  fname, "rel": rel, "index": index,
            }
        return {
            "status":      "ok",
            "entity_id":   result.get("entity_id"),
            "entity_name": result.get("entity_name") or fname,
            "file_hash":   file_hash,
            "size":        size,
            "fname":       fname, "rel": rel, "index": index,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "error":  str(exc)[:200],
            "fname":  fname, "rel": rel, "index": index,
        }


# Background task

async def run_distill_job(
    db_root:     Path,
    writer,
    index,
    model:       str,
    source:      str,
    concurrency: int = 1,
) -> None:
    """
    Async background task: read the queue, distil files in parallel batches,
    persist progress after each batch.  Checks the pause event between batches.

    ``concurrency`` controls how many files are distilled simultaneously.
    """
    from .distiller import ContentDistiller
    from .file_manifest import FileManifest

    key = str(db_root)

    info = read_distill_info(db_root)
    if not info:
        logger.error(f"[FolderDistiller] No {_INFO_FILE} at {db_root} — aborting task")
        return

    file_list   = _read_queue(db_root)
    next_index  = info.get("next_index", 0)
    processed   = info.get("processed",  0)
    failed      = info.get("failed",     0)
    skipped     = info.get("skipped",    0)
    failed_list = list(info.get("failed_list", []))
    log         = list(info.get("log", []))
    total       = len(file_list)
    concurrency = max(1, concurrency)

    file_manifest = FileManifest(db_root)
    distiller     = ContentDistiller(writer=writer, index=index, model=model, file_manifest=file_manifest)

    _save_progress(db_root, info, "running", next_index, processed, failed, skipped, failed_list, log)
    logger.info(f"[FolderDistiller] Starting from index {next_index}/{total} "
                f"(concurrency={concurrency})")

    i = next_index
    while i < total:

        # Pause check (between batches)
        ev = _pause_events.get(key)
        if ev is not None and not ev.is_set():
            _save_progress(db_root, info, "paused", i, processed, failed, skipped, failed_list, log)
            logger.info(f"[FolderDistiller] Paused at {i}/{total} "
                        f"(processed={processed} failed={failed} skipped={skipped})")
            _active_tasks.pop(key, None)
            _pause_events.pop(key, None)
            return

        # Build next batch: skip unsupported inline, collect up to N files
        batch: list[tuple[int, str, Path]] = []
        j = i
        while j < total and len(batch) < concurrency:
            rel = file_list[j]
            fp  = Path(info["folder_path"]) / rel
            if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped += 1
                j       += 1
                continue
            batch.append((j, rel, fp))
            j += 1

        if not batch:
            # Only unsupported files remained
            i = j
            _save_progress(db_root, info, "running", i, processed, failed, skipped, failed_list, log)
            await asyncio.sleep(0)
            continue

        # Signal which files are being processed
        current_names = " | ".join(fp.name for _, _, fp in batch)
        _save_progress(db_root, info, "running", i, processed, failed, skipped, failed_list, log, current_names)

        # Distil batch in parallel
        coros   = [_distill_one_file(distiller, source, fp, rel, idx) for idx, rel, fp in batch]
        results = await asyncio.gather(*coros)

        # Apply results
        for res in results:
            if res["status"] == "skip":
                skipped += 1
            elif res["status"] == "fail":
                failed += 1
                err = res.get("error", "unknown error")
                failed_list.append({"index": res["index"], "path": res["rel"], "error": err})
                log.append(f"✗ {res['fname']} — {err[:80]}")
                logger.debug(f"[FolderDistiller] [{res['index']}] FAIL {res['rel']}: {err}")
            else:
                processed += 1
                log.append(f"✓ {res['entity_name']} ← {res['fname']}")
                logger.debug(f"[FolderDistiller] [{res['index']}] OK   {res['rel']} → {res['entity_name']}")

        i = j
        _save_progress(db_root, info, "running", i, processed, failed, skipped, failed_list, log)
        await asyncio.sleep(0)   # yield to the event loop between batches

    _save_progress(db_root, info, "completed", total, processed, failed, skipped, failed_list, log)
    logger.info(
        f"[FolderDistiller] Completed {total} files — "
        f"processed={processed} failed={failed} skipped={skipped}"
    )
    _active_tasks.pop(key, None)
    _pause_events.pop(key, None)


# Pause (sync — safe to call from async context)

def pause_job(db_root: Path) -> dict:
    """
    Signal the running job to pause after the current file.
    If no task is in memory but the file says "running" (e.g. server restart),
    also corrects the file to "paused".
    """
    key = str(db_root)
    ev  = _pause_events.get(key)
    if ev:
        ev.clear()                       # clear = pause after current file
        return {"paused": True, "was_running": True}

    # Task not in memory — correct stale "running" status in file
    info = read_distill_info(db_root)
    if info.get("status") == "running":
        write_distill_info(db_root, {**info, "status": "paused", "last_updated": _now_iso()})
    return {"paused": True, "was_running": False}
