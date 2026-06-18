"""
core/aethviondb/importer.py
Import entities into an AethvionDB database from exported files.

Supported input formats
-----------------------
  jsonl     — one flat entity per line (from baker.py / .jsonl bake)
  json      — {baked_at, entities: [...]} wrapper (from baker.py / .json bake)
  json      — bare array of flat or Layer-1 entities
  json      — single Layer-1 entity object {id, name, type, status, sections, ...}

Flat vs Layer-1
---------------
Baked (flat) entities carry their data at the top level:
  {id, name, type, status, summary, aliases, tags, relations:[{target, target_id, ...}], ...}

Layer-1 entities use nested sections:
  {id, name, type, status, sections:{core:{summary,aliases,...}, relations:[{target_id,...}], ...}}

Both formats are detected automatically and normalised before writing.

Conflict modes
--------------
  skip      — if an entity with the same name already exists, leave it unchanged
  overwrite — replace the existing entity completely with the imported data
"""

from __future__ import annotations

import json
from typing import Any, Optional

from aethviondb._utils import get_logger
from .entity_schema import VALID_TYPES, _new_id, _now_iso

logger = get_logger(__name__)

CONFLICT_MODES = ("skip", "overwrite")

_VALID_STATUSES = {"active", "stub", "deleted"}


# Format detection

def detect_format(content: str, filename: str = "") -> str:
    """
    Return one of: 'jsonl', 'json_bake', 'json_array', 'json_single', 'unknown'.
    """
    fname = filename.lower()
    stripped = content.strip()

    if fname.endswith(".jsonl"):
        return "jsonl"

    if fname.endswith(".json") or not fname:
        # Try full-JSON parse first
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return "json_array"
            if isinstance(parsed, dict):
                if "entities" in parsed and isinstance(parsed["entities"], list):
                    return "json_bake"
                if "sections" in parsed and "id" in parsed:
                    return "json_single"
        except json.JSONDecodeError:
            pass

    # JSONL heuristic: multiple lines each parseable as JSON
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 1:
        try:
            json.loads(lines[0])
            return "jsonl"
        except json.JSONDecodeError:
            pass

    return "unknown"


# Normalisation helpers

def _is_flat(entity: dict) -> bool:
    """True when the entity is in baked (flat) format rather than Layer-1."""
    return "sections" not in entity


def _flat_to_layer1(flat: dict, source_override: Optional[str] = None) -> dict:
    """Convert a baked flat entity dict to Layer-1 format."""
    eid = flat.get("id", "") or _new_id()
    if not eid.startswith("ws_"):
        eid = "ws_" + eid

    etype = flat.get("type", "other")
    if etype not in VALID_TYPES:
        etype = "other"

    status = flat.get("status", "active")
    if status not in _VALID_STATUSES:
        status = "active"

    now = _now_iso()

    # Relations in baked format have {kind, target_id, target (name), note}
    # We keep them as-is; _resolve_relations() will normalise them later.
    raw_rels = flat.get("relations", [])

    return {
        "id":      eid,
        "name":    flat.get("name", ""),
        "type":    etype,
        "status":  status,
        "version": flat.get("version", 1),
        "created": flat.get("created", now),
        "updated": flat.get("updated", now),
        "source":  source_override or flat.get("source", "import"),
        "sections": {
            "core": {
                "summary":    flat.get("summary", ""),
                "aliases":    list(flat.get("aliases", [])),
                "categories": list(flat.get("categories", [])),
                "tags":       list(flat.get("tags", [])),
            },
            "timeline":   list(flat.get("timeline", [])),
            "relations":  list(raw_rels),
            "properties": dict(flat.get("properties", {})),
            "stubs":      list(flat.get("stubs", [])),
            "vectors":    dict(flat.get("vectors", {})),
        },
    }


def _normalise(entity: dict, source_override: Optional[str] = None) -> dict:
    """Return a Layer-1 entity dict regardless of input format."""
    if _is_flat(entity):
        return _flat_to_layer1(entity, source_override)

    # Already Layer-1; patch a few things
    entity = dict(entity)
    if source_override:
        entity["source"] = source_override

    eid = entity.get("id", "") or _new_id()
    if not eid.startswith("ws_"):
        eid = "ws_" + eid
    entity["id"] = eid

    if entity.get("type") not in VALID_TYPES:
        entity["type"] = "other"
    if entity.get("status") not in _VALID_STATUSES:
        entity["status"] = "active"

    if "sections" not in entity:
        entity["sections"] = {}
    sec = entity["sections"]
    sec.setdefault("core",       {"summary": "", "aliases": [], "categories": [], "tags": []})
    sec.setdefault("timeline",   [])
    sec.setdefault("relations",  [])
    sec.setdefault("properties", {})
    sec.setdefault("stubs",      [])

    return entity


# Relation resolution

def _resolve_relations(
    entity:         dict,
    import_name_map: dict[str, str],
    index,
    writer,
) -> list[dict]:
    """
    Resolve relation targets to IDs and return a clean relations list.

    Resolution order:
      1. target_id is present and valid (entity file exists in target DB)
      2. target_name in the current import batch
      3. target_name in the existing NameIndex
      4. Keep raw target_id even if entity file is absent (dangling ref, allowed)
    """
    resolved: list[dict] = []
    for rel in entity["sections"].get("relations", []):
        if not isinstance(rel, dict):
            continue
        kind        = rel.get("kind", "related_to") or "related_to"
        target_id   = rel.get("target_id", "") or ""
        # baked format carries the resolved name in "target"; Layer-1 doesn't
        target_name = rel.get("target", "") or rel.get("target_name", "") or ""
        note        = rel.get("note", "") or ""

        resolved_id: str = ""

        if target_id and writer.exists(target_id):
            resolved_id = target_id
        elif target_name and target_name in import_name_map:
            resolved_id = import_name_map[target_name]
        elif target_name:
            resolved_id = index.get(target_name) or ""
        elif target_id:
            # Dangling reference — keep it; entity may be imported later
            resolved_id = target_id

        if not resolved_id:
            continue

        entry: dict[str, Any] = {"kind": kind, "target_id": resolved_id}
        if note:
            entry["note"] = note
        resolved.append(entry)

    return resolved


# Parse

def parse_content(content: str, filename: str = "") -> tuple[str, list[dict]]:
    """
    Parse import content into (detected_format, list_of_raw_entity_dicts).
    Raises ValueError on parse failure.
    """
    fmt = detect_format(content, filename)
    if fmt == "unknown":
        raise ValueError(
            "Unrecognised format. Supported: .jsonl bake, .json bake, "
            "JSON entity array, or single entity JSON."
        )

    stripped = content.strip()
    raw: list[dict] = []

    if fmt == "jsonl":
        for i, line in enumerate(stripped.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    raw.append(obj)
            except json.JSONDecodeError as e:
                raise ValueError(f"JSONL parse error on line {i}: {e}") from e

    elif fmt == "json_bake":
        parsed = json.loads(stripped)
        raw = [e for e in parsed.get("entities", []) if isinstance(e, dict)]

    elif fmt == "json_array":
        parsed = json.loads(stripped)
        raw = [e for e in parsed if isinstance(e, dict)]

    elif fmt == "json_single":
        raw = [json.loads(stripped)]

    return fmt, raw


# Main import function

def import_entities(
    writer,
    index,
    content:         str,
    filename:        str            = "import.json",
    conflict_mode:   str            = "skip",
    source_override: Optional[str]  = None,
) -> dict[str, Any]:
    """
    Parse *content* and import entities into the database.

    Parameters
    ----------
    writer          : EntityWriter
    index           : NameIndex
    content         : raw file content (str)
    filename        : original filename (used for format detection hint)
    conflict_mode   : 'skip' | 'overwrite'
    source_override : if set, overrides the 'source' field on every entity

    Returns a report dict:
    {status, format, total, imported, skipped, failed, failed_list}
    """
    if conflict_mode not in CONFLICT_MODES:
        conflict_mode = "skip"

    # Parse
    try:
        fmt, raw_entities = parse_content(content, filename)
    except ValueError as exc:
        return {
            "status": "error",
            "error":  str(exc),
            "total": 0, "imported": 0, "skipped": 0, "failed": 0,
            "failed_list": [],
        }

    if not raw_entities:
        return {
            "status": "done", "format": fmt,
            "total": 0, "imported": 0, "skipped": 0, "failed": 0,
            "failed_list": [], "conflict_mode": conflict_mode,
        }

    # Normalise to Layer-1
    entities: list[dict] = []
    for raw in raw_entities:
        try:
            entities.append(_normalise(raw, source_override))
        except Exception as exc:
            logger.warning(f"[Importer] Normalise failed: {exc}")

    total = len(entities)

    # Build name→id map for the import batch so self-referential relations resolve
    import_name_map: dict[str, str] = {
        e["name"]: e["id"] for e in entities if e.get("name")
    }

    imported     = 0
    skipped      = 0
    failed       = 0
    failed_list: list[dict] = []

    for entity in entities:
        name = entity.get("name", "").strip()
        if not name:
            failed += 1
            failed_list.append({"name": "(no name)", "error": "Entity has no name"})
            continue

        existing_id = index.get(name)

        if existing_id and conflict_mode == "skip":
            skipped += 1
            continue

        # Resolve relations
        try:
            entity["sections"]["relations"] = _resolve_relations(
                entity, import_name_map, index, writer
            )
        except Exception as exc:
            logger.warning(f"[Importer] Relation resolution failed for {name!r}: {exc}")

        try:
            if existing_id and conflict_mode == "overwrite":
                # Replace existing entity
                entity["id"] = existing_id
                existing     = writer.get(existing_id) or {}
                entity["version"] = existing.get("version", 0) + 1
                entity["updated"] = _now_iso()
                writer._write(entity)
                index.register(name, existing_id)
            else:
                # New entity — guard against ID collision
                if writer.exists(entity["id"]):
                    entity["id"] = _new_id()
                writer._write(entity)
                index.register(name, entity["id"])

            # Register aliases
            aliases = entity["sections"]["core"].get("aliases", [])
            if aliases:
                index.register_aliases(entity["id"], aliases)

            imported += 1
            logger.debug(f"[Importer] ✓ {name!r} ({entity['id']})")

        except Exception as exc:
            failed += 1
            err = str(exc)[:200]
            failed_list.append({"name": name, "error": err})
            logger.warning(f"[Importer] ✗ {name!r}: {err}")

    logger.info(
        f"[Importer] Done — imported={imported} skipped={skipped} "
        f"failed={failed} total={total} fmt={fmt}"
    )
    return {
        "status":        "done",
        "format":        fmt,
        "total":         total,
        "imported":      imported,
        "skipped":       skipped,
        "failed":        failed,
        "failed_list":   failed_list[:50],
        "conflict_mode": conflict_mode,
    }
