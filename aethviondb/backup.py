"""
core/aethviondb/backup.py
Create, list, restore and delete database backups.

A backup is a folder snapshot stored under:
  {db_root}/backups/{backup_id}/

Where backup_id is:  {YYYYMMDD_HHMMSS}_{label}
  e.g.  20240115_143022_before-merge
  or    20240115_143022   (when no label is given)

Each backup folder contains:
  entities/            — copy of all entity JSON files
  name_index.json      — copy of the name index
  AethvionDB.BACKUP    — JSON metadata file

AethvionDB.BACKUP schema
------------------------
{
  "backup_id":    "20240115_143022_before-merge",
  "label":        "before-merge",
  "created":      "2024-01-15T14:30:22+00:00",
  "db_name":      "default",
  "entity_count": 42,
  "size_bytes":   123456
}
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from aethviondb._utils import get_logger

logger = get_logger(__name__)

_BACKUP_META_FILE = "AethvionDB.BACKUP"


# Internal helpers

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backups_dir(db_root: Path) -> Path:
    return db_root / "backups"


def _read_backup_meta(backup_dir: Path) -> dict | None:
    meta_file = backup_dir / _BACKUP_META_FILE
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _dir_size(path: Path) -> int:
    """Return total byte size of all files under *path*."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


# Public API

def create_backup(
    db_root: Path,
    db_name: str,
    label:   str = "",
) -> dict:
    """Create a point-in-time snapshot of *db_root*.

    Copies the ``entities/`` directory and ``name_index.json`` into a new
    timestamped folder under ``{db_root}/backups/``.

    Returns the AethvionDB.BACKUP metadata dict.
    Raises RuntimeError on failure (partial backup is cleaned up).
    """
    stamp      = _now_stamp()
    safe_label = label.strip().replace(" ", "-")[:40] if label.strip() else ""
    backup_id  = f"{stamp}_{safe_label}" if safe_label else stamp

    backup_dir = _backups_dir(db_root) / backup_id
    if backup_dir.exists():
        raise RuntimeError(f"Backup folder already exists: {backup_dir}")

    backup_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Copy entities dir
        src_entities = db_root / "entities"
        dst_entities = backup_dir / "entities"
        if src_entities.exists():
            shutil.copytree(src_entities, dst_entities)
        else:
            dst_entities.mkdir()

        # Copy name index
        src_index = db_root / "name_index.json"
        if src_index.exists():
            shutil.copy2(src_index, backup_dir / "name_index.json")

        # Count and measure
        entity_count = sum(1 for _ in dst_entities.glob("ws_*.json")) if dst_entities.exists() else 0
        size_bytes   = _dir_size(backup_dir)

        meta = {
            "backup_id":    backup_id,
            "label":        label.strip(),
            "created":      _now_iso(),
            "db_name":      db_name,
            "entity_count": entity_count,
            "size_bytes":   size_bytes,
        }
        (backup_dir / _BACKUP_META_FILE).write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            f"[Backup] Created {backup_id!r} for db={db_name!r} "
            f"({entity_count} entities, {size_bytes} bytes)"
        )
        return meta

    except Exception as exc:
        # Remove partial backup on failure
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise RuntimeError(f"Backup failed: {exc}") from exc


def list_backups(db_root: Path) -> list[dict]:
    """Return all backups for *db_root*, newest first.

    Each item is the AethvionDB.BACKUP metadata dict.
    """
    backups_dir = _backups_dir(db_root)
    if not backups_dir.exists():
        return []

    result: list[dict] = []
    for d in backups_dir.iterdir():
        if not d.is_dir():
            continue
        meta = _read_backup_meta(d)
        if meta is not None:
            result.append(meta)

    result.sort(key=lambda m: m.get("created", ""), reverse=True)
    return result


def restore_backup(db_root: Path, backup_id: str) -> dict:
    """Restore *backup_id*, replacing the current database contents.

    Returns a report dict ``{restored, backup_id, entity_count}``.
    Raises RuntimeError if the backup is not found or restore fails.
    """
    backup_dir = _backups_dir(db_root) / backup_id
    if not backup_dir.exists():
        raise RuntimeError(f"Backup not found: {backup_id!r}")

    meta = _read_backup_meta(backup_dir)
    if meta is None:
        raise RuntimeError(f"Backup metadata missing for {backup_id!r}")

    try:
        # Replace entities dir
        dest_entities = db_root / "entities"
        if dest_entities.exists():
            shutil.rmtree(dest_entities)
        src_entities = backup_dir / "entities"
        if src_entities.exists():
            shutil.copytree(src_entities, dest_entities)
        else:
            dest_entities.mkdir(parents=True)

        # Replace name index
        src_index  = backup_dir / "name_index.json"
        dest_index = db_root / "name_index.json"
        if src_index.exists():
            shutil.copy2(src_index, dest_index)
        elif dest_index.exists():
            dest_index.unlink()

        # Entity files were replaced wholesale outside the normal write path —
        # drop the in-memory + on-disk cache so the next read rebuilds.
        try:
            from . import snapshot as _snapshot
            _snapshot.invalidate(db_root)
        except Exception as exc:
            logger.debug(f"[Backup] Cache invalidate after restore failed: {exc}")

        entity_count = (
            sum(1 for _ in dest_entities.glob("ws_*.json"))
            if dest_entities.exists() else 0
        )
        logger.info(f"[Backup] Restored {backup_id!r} → {db_root} ({entity_count} entities)")
        return {"restored": True, "backup_id": backup_id, "entity_count": entity_count}

    except Exception as exc:
        raise RuntimeError(f"Restore failed: {exc}") from exc


def delete_backup(db_root: Path, backup_id: str) -> bool:
    """Delete a backup folder.

    Returns True if deleted, False if the backup was not found.
    """
    backup_dir = _backups_dir(db_root) / backup_id
    if not backup_dir.exists():
        return False
    shutil.rmtree(backup_dir)
    logger.info(f"[Backup] Deleted backup {backup_id!r}")
    return True


def prune_backups(db_root: Path, keep_count: int) -> list[str]:
    """Delete oldest backups, keeping at most *keep_count* newest.

    Returns a list of deleted backup_ids.
    """
    if keep_count <= 0:
        return []

    backups  = list_backups(db_root)   # newest first
    to_prune = backups[keep_count:]
    deleted: list[str] = []
    for meta in to_prune:
        bid = meta.get("backup_id", "")
        if bid and delete_backup(db_root, bid):
            deleted.append(bid)

    if deleted:
        logger.info(f"[Backup] Pruned {len(deleted)} backup(s): {deleted}")
    return deleted
