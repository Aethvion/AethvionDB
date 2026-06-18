"""
core/aethviondb/api_v1/baked_routes.py
AethvionDB v1 API — /baked/ endpoints.

Operations on baked dataset snapshots:
  list, trigger, get-meta, search, paginated-entities, delete, rename, download.

Baked files are read from  <db_root>/baked/<name>.<ext>.
JSONL and JSON formats support full entity loading + search.
Markdown and TXT support metadata listing only.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aethviondb._utils import get_logger

from .auth import check_auth
from .response import envelope, encode_cursor, decode_cursor

logger = get_logger(__name__)
router = APIRouter()

_SAFE_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


# Helpers

def _root(db: str) -> Path:
    if not _SAFE_RE.match(db):
        raise HTTPException(400, f"Invalid database name {db!r}")
    from aethviondb.db_registry import resolve_db_root
    return resolve_db_root(db)


def _load_entities(meta: dict) -> list[dict]:
    """Load all entities from a bake output file. Returns [] for unsupported formats."""
    fmt      = meta.get("format", "jsonl")
    out_path = Path(meta.get("output_path", ""))
    if not out_path.exists():
        return []

    if fmt == "jsonl":
        entities = []
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entities.append(json.loads(line))
                except Exception:
                    pass
        return entities

    if fmt == "json":
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            return data.get("entities", [])
        except Exception:
            return []

    return []   # markdown / txt — not indexable


def _bake_keyword_score(query: str, entity: dict) -> float:
    if not query:
        return 1.0
    q       = query.lower()
    name    = entity.get("name", "").lower()
    summary = entity.get("summary", "").lower()
    tags    = " ".join(entity.get("tags", [])).lower()
    aliases = " ".join(entity.get("aliases", [])).lower()

    if name == q:           return 1.0
    if q in name:           return 0.90
    if q in summary[:400]:  return 0.70
    if q in aliases:        return 0.65
    if q in tags:           return 0.60

    words = q.split()
    if len(words) > 1:
        haystack = f"{name} {summary[:600]} {aliases} {tags}"
        matched  = sum(1 for w in words if w in haystack)
        if matched:
            return round(0.5 * matched / len(words), 3)
    return 0.0


def _bake_filter(entities: list[dict], filters: dict) -> list[dict]:
    if not filters:
        return entities

    def _as_list(v): return v if isinstance(v, list) else [v]

    result = []
    for e in entities:
        if "type"   in filters and e.get("type")   not in _as_list(filters["type"]):   continue
        if "status" in filters and e.get("status") not in _as_list(filters["status"]): continue
        tf = filters.get("tags")
        if tf and not any(t in e.get("tags", []) for t in _as_list(tf)):
            continue
        result.append(e)
    return result


# Request schemas

class TriggerBakeRequest(BaseModel):
    name:            str       = "default"
    format:          str       = "jsonl"
    include_stubs:   bool      = True
    include_vectors: bool      = False
    vector_models:   list[str] = []


class RenameRequest(BaseModel):
    new_name: str


class BakeSearchRequest(BaseModel):
    query:   str  = ""
    filters: dict = {}
    limit:   int  = 20


# List / trigger

@router.get("/{db}/baked")
async def list_bakes(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import list_bakes as _list
    bakes = _list(root)
    return envelope({"bakes": bakes, "total": len(bakes)}, db=db, took_start=t)


@router.post("/{db}/baked")
async def trigger_bake(
    db:  str,
    req: TriggerBakeRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import (
        _bake_tasks, _bake_current_name, bake_database,
        is_baking, BAKE_FORMATS, safe_name,
    )
    from aethviondb.entity_writer import EntityWriter

    if is_baking(root):
        raise HTTPException(409, "A bake is already running for this database.")
    if req.format not in BAKE_FORMATS:
        raise HTTPException(400, f"Unknown format {req.format!r}. Valid: {BAKE_FORMATS}")
    if not safe_name(req.name):
        raise HTTPException(400, "Bake name must be 1-64 chars: letters, digits, _ or -")

    writer = EntityWriter(entities_dir=root / "entities")
    key    = str(root)
    _bake_current_name[key] = req.name

    task = asyncio.create_task(
        bake_database(
            root, writer,
            name=req.name,
            fmt=req.format,
            include_stubs=req.include_stubs,
            include_vectors=req.include_vectors,
            vector_models=req.vector_models or None,
        )
    )
    _bake_tasks[key] = task
    task.add_done_callback(
        lambda _: (_bake_tasks.pop(key, None), _bake_current_name.pop(key, None))
    )

    return envelope(
        {"started": True, "name": req.name, "format": req.format},
        db=db, took_start=t,
    )


# Individual bake

@router.get("/{db}/baked/{name}")
async def get_bake(
    db:   str,
    name: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import read_bake_meta
    meta = read_bake_meta(root, name)
    if not meta:
        raise HTTPException(404, f"Bake {name!r} not found.")
    return envelope(meta, db=db, took_start=t)


@router.delete("/{db}/baked/{name}")
async def delete_bake(
    db:   str,
    name: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import delete_bake as _delete, is_baking, current_bake_name
    if is_baking(root) and current_bake_name(root) == name:
        raise HTTPException(409, f"Bake {name!r} is currently running.")
    if not _delete(root, name):
        raise HTTPException(404, f"Bake {name!r} not found.")
    return envelope({"deleted": name}, db=db, took_start=t)


@router.patch("/{db}/baked/{name}")
async def rename_bake(
    db:   str,
    name: str,
    req:  RenameRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import rename_bake as _rename, safe_name
    if not safe_name(req.new_name):
        raise HTTPException(400, "New name must be 1-64 chars: letters, digits, _ or -")
    if not _rename(root, name, req.new_name):
        raise HTTPException(404, f"Bake {name!r} not found.")
    return envelope({"renamed": True, "old_name": name, "new_name": req.new_name}, db=db, took_start=t)


# Entities from snapshot

@router.get("/{db}/baked/{name}/entities")
async def bake_entities(
    db:     str,
    name:   str,
    limit:  int = Query(100, le=500),
    cursor: Optional[str] = Query(None),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import read_bake_meta
    meta = read_bake_meta(root, name)
    if not meta or meta.get("status") != "done":
        raise HTTPException(404, f"Bake {name!r} not found or not completed.")

    entities = await asyncio.to_thread(_load_entities, meta)
    total    = len(entities)
    offset   = decode_cursor(cursor) if cursor else 0
    page     = entities[offset : offset + limit]
    next_c   = encode_cursor(offset + limit) if (offset + limit) < total else None

    return envelope(
        {"entities": page, "total": total, "returned": len(page), "offset": offset},
        db=db, took_start=t, cursor=next_c,
    )


# Search within snapshot

@router.post("/{db}/baked/{name}/search")
async def bake_search(
    db:   str,
    name: str,
    req:  BakeSearchRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import read_bake_meta
    meta = read_bake_meta(root, name)
    if not meta or meta.get("status") != "done":
        raise HTTPException(404, f"Bake {name!r} not found or not completed.")
    if meta.get("format") not in ("jsonl", "json"):
        raise HTTPException(400, "Search is only supported for JSONL and JSON bakes.")

    entities = await asyncio.to_thread(_load_entities, meta)
    entities = _bake_filter(entities, req.filters)

    scored: list[dict] = []
    for e in entities:
        score = _bake_keyword_score(req.query, e)
        if score > 0 or not req.query:
            scored.append({**e, "score": round(score, 3)})

    scored.sort(key=lambda r: r.get("score", 0), reverse=True)

    return envelope(
        {
            "bake":           name,
            "results":        scored[: req.limit],
            "total_matched":  len(scored),
            "total_scanned":  len(entities),
        },
        db=db, took_start=t,
    )


# Download

@router.get("/{db}/baked/{name}/download")
async def download_bake(
    db:   str,
    name: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)

    from aethviondb.baker import read_bake_meta
    meta = read_bake_meta(root, name)
    if not meta or meta.get("status") != "done":
        raise HTTPException(404, f"Bake {name!r} not found or not completed.")

    out_path = Path(meta["output_path"])
    if not out_path.exists():
        raise HTTPException(404, f"Bake file missing: {out_path.name}")

    media = {
        "jsonl":    "application/x-ndjson",
        "json":     "application/json",
        "markdown": "text/markdown",
        "txt":      "text/plain",
    }.get(meta.get("format", ""), "application/octet-stream")

    return FileResponse(path=str(out_path), media_type=media, filename=out_path.name)
