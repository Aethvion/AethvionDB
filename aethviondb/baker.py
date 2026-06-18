"""
core/aethviondb/baker.py
Bake an AethvionDB database into a single optimised export file.

All baked files live in  db_root/baked/
Each bake has:
  • An output file   — baked/<name>.<ext>
  • A metadata file  — baked/<name>.meta.json

Multiple named bakes are fully supported; they coexist independently.

Supported output formats
------------------------
  jsonl     — one entity JSON object per line (streaming-friendly, vector-DB ready)
  json      — single JSON document {meta, entities:[...]}
  markdown  — structured Markdown (good for RAG / LLM prompts)
  txt       — compact plain-text (maximum density for context windows)
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aethviondb._utils import get_logger

if TYPE_CHECKING:
    from .entity_writer import EntityWriter

logger = get_logger(__name__)

BAKE_FORMATS   = ("jsonl", "json", "markdown", "txt")
_BAKED_DIR     = "baked"
_META_SUFFIX   = ".meta.json"
_SAFE_NAME_RE  = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

# In-process task tracking

_bake_tasks:        dict[str, asyncio.Task] = {}   # str(db_root) → Task
_bake_current_name: dict[str, str]          = {}   # str(db_root) → bake name


def is_baking(db_root: Path) -> bool:
    return str(db_root) in _bake_tasks


def current_bake_name(db_root: Path) -> str | None:
    return _bake_current_name.get(str(db_root))


# Path helpers

def bake_dir(db_root: Path) -> Path:
    return db_root / _BAKED_DIR


def bake_output_path(db_root: Path, name: str, fmt: str) -> Path:
    ext = {"json": ".json", "jsonl": ".jsonl", "markdown": ".md", "txt": ".txt"}.get(fmt, ".jsonl")
    return bake_dir(db_root) / f"{name}{ext}"


def bake_meta_path(db_root: Path, name: str) -> Path:
    return bake_dir(db_root) / f"{name}{_META_SUFFIX}"


def safe_name(name: str) -> bool:
    return bool(_SAFE_NAME_RE.match(name))


# Helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_size(b: int) -> str:
    if b < 1024:       return f"{b} B"
    if b < 1024 ** 2:  return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:  return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


# Metadata helpers

def read_bake_meta(db_root: Path, name: str) -> dict:
    """Return the metadata for a named bake, or {} if absent."""
    p = bake_meta_path(db_root, name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_bake_meta(db_root: Path, name: str, data: dict) -> None:
    """Persist metadata for a named bake (best-effort, never raises)."""
    bake_dir(db_root).mkdir(parents=True, exist_ok=True)
    try:
        bake_meta_path(db_root, name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"[Baker] Could not write meta for {name!r}: {exc}")


def list_bakes(db_root: Path) -> list[dict]:
    """Return metadata for all bakes in baked/, newest-first."""
    bd = bake_dir(db_root)
    if not bd.exists():
        return []
    bakes: list[dict] = []
    for p in bd.glob(f"*{_META_SUFFIX}"):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            bakes.append(meta)
        except Exception:
            pass
    bakes.sort(key=lambda b: b.get("baked_at", b.get("started_at", "")), reverse=True)
    return bakes


def delete_bake(db_root: Path, name: str) -> bool:
    """Delete the output file and meta for a named bake. Returns True if found."""
    found = False
    for fmt in BAKE_FORMATS:
        out = bake_output_path(db_root, name, fmt)
        if out.exists():
            out.unlink()
            found = True
    mp = bake_meta_path(db_root, name)
    if mp.exists():
        mp.unlink()
        found = True
    return found


def rename_bake(db_root: Path, old_name: str, new_name: str) -> bool:
    """
    Rename a bake's output file and meta.
    Returns True on success, False if the bake does not exist.
    """
    meta = read_bake_meta(db_root, old_name)
    if not meta:
        return False
    fmt     = meta.get("format", "jsonl")
    old_out = bake_output_path(db_root, old_name, fmt)
    new_out = bake_output_path(db_root, new_name, fmt)
    if old_out.exists():
        old_out.rename(new_out)
    meta["name"]        = new_name
    meta["output_file"] = new_out.name
    meta["output_path"] = str(new_out)
    write_bake_meta(db_root, new_name, meta)
    mp = bake_meta_path(db_root, old_name)
    if mp.exists():
        mp.unlink()
    return True


# Entity flattening

def _flatten(
    entity:          dict,
    id_to_name:      dict[str, str],
    include_vectors: bool             = False,
    vector_models:   list[str] | None = None,
) -> dict:
    """
    Flatten the layered entity schema into a single portable dict.
    Relation target_ids are resolved to names where possible.

    vector_models : None   → include all stored models (when include_vectors=True)
                    [...]  → include only the listed model keys
    """
    sec  = entity.get("sections", {})
    core = sec.get("core", {})

    relations = []
    for r in sec.get("relations", []):
        tid = r.get("target_id", "")
        relations.append({
            "kind":      r.get("kind", ""),
            "target_id": tid,
            "target":    id_to_name.get(tid, tid),
            "note":      r.get("note", ""),
        })

    if include_vectors:
        all_vecs = sec.get("vectors", {})
        vectors  = {k: v for k, v in all_vecs.items() if k in vector_models} if vector_models else all_vecs
    else:
        vectors = {}

    return {
        "id":         entity.get("id",      ""),
        "name":       entity.get("name",    ""),
        "type":       entity.get("type",    "other"),
        "status":     entity.get("status",  "active"),
        "version":    entity.get("version", 1),
        "created":    entity.get("created", ""),
        "updated":    entity.get("updated", ""),
        "source":     entity.get("source",  ""),
        "summary":    core.get("summary",    ""),
        "aliases":    core.get("aliases",    []),
        "categories": core.get("categories", []),
        "tags":        core.get("tags",       []),
        "relations":   relations,
        "timeline":    sec.get("timeline",   []),
        "properties":  sec.get("properties", {}),
        "stubs":       sec.get("stubs",      []),
        "vectors":     vectors,
    }


# Format renderers

def _render_jsonl(entities: list[dict]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entities) + "\n"


def _render_json(entities: list[dict], db_root: Path) -> str:
    return json.dumps(
        {
            "baked_at":     _now_iso(),
            "database":     db_root.name,
            "entity_count": len(entities),
            "entities":     entities,
        },
        indent=2,
        ensure_ascii=False,
    )


def _render_markdown(entities: list[dict], db_root: Path) -> str:
    lines = [
        f"# AethvionDB — {db_root.name}",
        "",
        f"Baked: {_now_iso()} · Entities: {len(entities)}",
        "",
        "---",
        "",
    ]
    for e in entities:
        lines.append(f"## {e['name']}")
        lines.append(f"*{e['type']} · {e['status']}*")
        lines.append("")
        if e["summary"]:
            lines.append(e["summary"])
            lines.append("")
        meta = []
        if e["aliases"]:    meta.append(f"**Aliases:** {', '.join(e['aliases'])}")
        if e["tags"]:       meta.append(f"**Tags:** {', '.join(e['tags'])}")
        if e["categories"]: meta.append(f"**Categories:** {', '.join(e['categories'])}")
        lines.extend(meta)
        if meta:
            lines.append("")
        if e["relations"]:
            lines.append("### Relations")
            for r in e["relations"]:
                note = f" — {r['note']}" if r.get("note") else ""
                lines.append(f"- {r['kind']}: **{r['target']}**{note}")
            lines.append("")
        if e["timeline"]:
            lines.append("### Timeline")
            for t in e["timeline"]:
                lines.append(f"- {t.get('date', '?')}: {t.get('event', '')}")
            lines.append("")
        if e["properties"]:
            lines.append("### Properties")
            for k, v in e["properties"].items():
                lines.append(f"- {k}: {v}")
            lines.append("")
        if e["stubs"]:
            lines.append("### Stubs")
            for s in e["stubs"]:
                lines.append(f"- {s}")
            lines.append("")
        lines += ["---", ""]
    return "\n".join(lines)


def _render_txt(entities: list[dict], db_root: Path) -> str:
    lines = [
        f"AethvionDB Export: {db_root.name}",
        f"Baked: {_now_iso()} | Entities: {len(entities)}",
        "=" * 60,
        "",
    ]
    for e in entities:
        lines.append(f"[{e['name']} | {e['type']} | {e['status']}]")
        if e["summary"]:
            lines.append(f"Summary: {e['summary']}")
        if e["aliases"]:
            lines.append(f"Aliases: {', '.join(e['aliases'])}")
        if e["tags"]:
            lines.append(f"Tags: {', '.join(e['tags'])}")
        if e["relations"]:
            rels = "; ".join(f"{r['kind']}:{r['target']}" for r in e["relations"])
            lines.append(f"Relations: {rels}")
        if e["timeline"]:
            tl = "; ".join(f"{t.get('date', '?')}:{t.get('event', '')}" for t in e["timeline"])
            lines.append(f"Timeline: {tl}")
        if e["properties"]:
            props = "; ".join(f"{k}={v}" for k, v in e["properties"].items())
            lines.append(f"Properties: {props}")
        if e["stubs"]:
            lines.append(f"Stubs: {', '.join(e['stubs'])}")
        lines.append("")
    return "\n".join(lines)


# Core bake logic (sync — run via asyncio.to_thread)

def bake_sync(
    db_root:         Path,
    writer:          "EntityWriter",
    name:            str             = "default",
    fmt:             str             = "jsonl",
    include_stubs:   bool            = True,
    include_vectors: bool            = False,
    vector_models:   list[str] | None = None,
) -> dict:
    """
    Read all entities, render the chosen format, write the output file and
    update the per-bake meta file.  Runs in a thread (can be slow for large DBs).
    Returns the bake-info dict on success.
    """
    if fmt not in BAKE_FORMATS:
        raise ValueError(f"Unknown format {fmt!r}; must be one of {BAKE_FORMATS}")

    bake_dir(db_root).mkdir(parents=True, exist_ok=True)

    # Mark as running
    write_bake_meta(db_root, name, {
        "name": name, "status": "running", "started_at": _now_iso(),
        "format": fmt, "include_vectors": include_vectors,
        "vector_models": vector_models or [],
    })

    all_entities = writer.list_all(include_deleted=False)
    if not include_stubs:
        all_entities = [e for e in all_entities if e.get("status") != "stub"]

    # Build id → name map for relation resolution
    id_to_name = {e["id"]: e["name"] for e in all_entities}

    flat = [_flatten(e, id_to_name, include_vectors=include_vectors, vector_models=vector_models) for e in all_entities]

    if   fmt == "jsonl":    content = _render_jsonl(flat)
    elif fmt == "json":     content = _render_json(flat, db_root)
    elif fmt == "markdown": content = _render_markdown(flat, db_root)
    else:                   content = _render_txt(flat, db_root)

    out_path = bake_output_path(db_root, name, fmt)
    out_path.write_text(content, encoding="utf-8")
    size_bytes = out_path.stat().st_size

    info = {
        "name":            name,
        "status":          "done",
        "format":          fmt,
        "include_stubs":   include_stubs,
        "include_vectors": include_vectors,
        "vector_models":   vector_models or [],
        "entity_count":    len(flat),
        "baked_at":        _now_iso(),
        "output_file":     out_path.name,
        "output_path":     str(out_path),
        "size_bytes":      size_bytes,
        "size_fmt":        _fmt_size(size_bytes),
    }
    write_bake_meta(db_root, name, info)
    logger.info(f"[Baker] Baked {name!r}: {len(flat)} entities → {out_path.name} ({_fmt_size(size_bytes)})")
    return info


# Public async entry point

async def bake_database(
    db_root:         Path,
    writer:          "EntityWriter",
    name:            str             = "default",
    fmt:             str             = "jsonl",
    include_stubs:   bool            = True,
    include_vectors: bool            = False,
    vector_models:   list[str] | None = None,
) -> None:
    """Async wrapper: runs bake_sync in a thread, updates meta on failure."""
    key = str(db_root)
    try:
        await asyncio.to_thread(bake_sync, db_root, writer, name, fmt, include_stubs, include_vectors, vector_models)
    except Exception as exc:
        logger.error(f"[Baker] Bake {name!r} failed for {db_root}: {exc}")
        write_bake_meta(db_root, name, {
            "name":      name,
            "status":    "error",
            "error":     str(exc)[:300],
            "failed_at": _now_iso(),
        })
    finally:
        _bake_tasks.pop(key, None)
        _bake_current_name.pop(key, None)
