"""
core/aethviondb/api_v1/raw_routes.py
AethvionDB v1 API — /raw/ endpoints.

Operations on the live fractal database:
  CRUD + smart upsert, hybrid search, graph traversal,
  AI distillation, vector similarity, batch operations.

All responses are wrapped in the standard envelope by calling envelope().
Timing is started at the top of each handler with time.perf_counter().
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from aethviondb._utils import get_logger

from .auth import check_auth
from .response import envelope, encode_cursor, decode_cursor

logger = get_logger(__name__)
router = APIRouter()

_SAFE_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


# Shared helpers

def _root(db: str) -> Path:
    if not _SAFE_RE.match(db):
        raise HTTPException(400, f"Invalid database name {db!r}")
    from aethviondb.db_registry import resolve_db_root
    return resolve_db_root(db)


def _writer(db: str):
    from aethviondb.entity_writer import EntityWriter
    root = _root(db)
    # Use the per-database name index — NOT the module singleton, which points at
    # the default database and would break dedup / get_by_name for every other db.
    return EntityWriter(entities_dir=root / "entities", index=_index(db))


def _index(db: str):
    from aethviondb.name_index import NameIndex
    root = _root(db)
    return NameIndex(index_path=root / "name_index.json")


def _ensure(db: str) -> None:
    root = _root(db)
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "chunks").mkdir(parents=True, exist_ok=True)


def _emit(db: str, action: str, entity: dict, actor: Optional[str]) -> None:
    """Publish a change event to the live feed. Best-effort; never raises.

    *actor* is who made the change — the X-Actor header an agent sends, falling
    back to the entity's source.
    """
    try:
        from aethviondb import events
        events.publish(db, {
            "action":      action,                       # created | updated | deleted
            "db":          db,
            "id":          entity.get("id"),
            "name":        entity.get("name"),
            "entity_type": entity.get("type"),
            "kind":        entity.get("kind"),
            "status":      entity.get("status"),
            "version":     entity.get("version"),
            "actor":       actor or entity.get("source") or "api",
            "ts":          datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    except Exception:
        pass


# Keyword + filter helpers

def _keyword_score(query: str, entity: dict) -> float:
    if not query:
        return 1.0
    q   = query.lower()
    sec = entity.get("sections", {}).get("core", {})
    name    = entity.get("name", "").lower()
    summary = sec.get("summary", "").lower()
    aliases = " ".join(sec.get("aliases", [])).lower()
    tags    = " ".join(sec.get("tags",    [])).lower()

    if name == q:              return 1.0
    if q in name:              return 0.90
    if q in summary[:400]:    return 0.70
    if q in aliases:           return 0.65
    if q in tags:              return 0.60

    words   = q.split()
    if len(words) > 1:
        haystack = f"{name} {summary[:600]} {aliases} {tags}"
        matched  = sum(1 for w in words if w in haystack)
        if matched:
            return round(0.5 * matched / len(words), 3)

    return 0.0


def _apply_filters(entities: list[dict], filters: dict) -> list[dict]:
    """Hard-filter entities by type, status, tags, and properties."""
    if not filters:
        return entities

    def _as_list(v) -> list:
        return v if isinstance(v, list) else [v]

    result = []
    for e in entities:
        if "type" in filters and e.get("type") not in _as_list(filters["type"]):
            continue
        if "status" in filters and e.get("status") not in _as_list(filters["status"]):
            continue
        kf = filters.get("kind")
        if kf:
            ek = e.get("kind")
            kinds_filter = _as_list(kf)
            if isinstance(ek, list):
                if not any(k in ek for k in kinds_filter):
                    continue
            elif ek not in kinds_filter:
                continue
        tf = filters.get("tags")
        if tf:
            etags = (e.get("sections") or {}).get("core", {}).get("tags", [])
            if not any(t in etags for t in _as_list(tf)):
                continue
        pf = filters.get("properties") or {}
        if pf:
            eprops = (e.get("sections") or {}).get("properties", {})
            if not all(eprops.get(k) == v for k, v in pf.items()):
                continue
        result.append(e)
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _flat_summary(entity: dict) -> dict:
    """Compact read-only summary for list/search responses."""
    sec = entity.get("sections", {})
    return {
        "id":              entity.get("id"),
        "name":            entity.get("name"),
        "type":            entity.get("type"),
        "status":          entity.get("status"),
        "version":         entity.get("version"),
        "summary":         sec.get("core", {}).get("summary", "")[:200],
        "tags":            sec.get("core", {}).get("tags", [])[:8],
        "relations_count": len(sec.get("relations", [])),
        "updated":         entity.get("updated"),
    }


def _strip_embeddings(entity: dict) -> dict:
    """Remove raw float arrays from the vectors section of an entity.

    The embedding arrays (e.g. 1,536 floats per model) are only needed for
    vector similarity operations, not for list/browse responses.  Stripping
    them keeps list payloads small — the metadata (model name, dimensions,
    generated_at, input preview) is preserved and is sufficient for display.

    Full vectors are returned by the single-entity endpoint:
      GET /{db}/raw/entities/{id}
    """
    sections = entity.get("sections")
    if not sections:
        return entity

    vectors = sections.get("vectors")
    if not vectors:
        return entity

    stripped: dict = {}
    for model_key, vdata in vectors.items():
        if isinstance(vdata, dict):
            # Keep everything except the raw float array
            stripped[model_key] = {k: v for k, v in vdata.items() if k != "embedding"}
            # Always report dimension count even if the array was already absent
            if "dimensions" not in stripped[model_key]:
                arr = vdata.get("embedding", [])
                stripped[model_key]["dimensions"] = len(arr) if isinstance(arr, list) else 0
        else:
            # Unexpected shape — keep as-is
            stripped[model_key] = vdata

    return {
        **entity,
        "sections": {**sections, "vectors": stripped},
    }


# Request schemas

class RelationInput(BaseModel):
    kind:        str
    target_name: Optional[str] = None   # resolved via name index
    target_id:   Optional[str] = None   # direct ID (fallback)
    note:        str = ""


class UpsertRequest(BaseModel):
    name:       str
    type:       str = "other"
    kind:       Optional[str] = None
    status:     Optional[str] = None
    source:     str = "api"
    summary:    Optional[str] = None
    aliases:    Optional[list[str]] = None
    tags:       Optional[list[str]] = None
    categories: Optional[list[str]] = None
    properties: Optional[dict[str, str]] = None
    relations:  Optional[list[RelationInput]] = None


class BatchOp(BaseModel):
    op:        str                        # upsert | delete | patch
    data:      Optional[dict] = None      # for upsert
    id:        Optional[str]  = None      # for delete / patch
    mutations: Optional[dict] = None      # for patch
    hard:      bool           = False     # for delete


class BatchRequest(BaseModel):
    operations: list[BatchOp]
    atomic:     bool = False


class HybridSearchRequest(BaseModel):
    query:        str  = ""
    modes:        list[str]        = ["keyword"]   # keyword | vector | metadata
    vector_model: Optional[str]    = None
    filters:      dict             = {}
    graph_expand: Optional[dict]   = None   # {depth, relation_kinds}
    limit:        int              = 20
    min_score:    float            = 0.0
    cursor:       Optional[str]    = None


class VectorSearchRequest(BaseModel):
    query:  Optional[str]         = None   # embed on server
    vector: Optional[list[float]] = None   # pre-embedded
    model:  str                   = "text-embedding-3-small"
    top_k:  int                   = 10
    filters: dict                 = {}


class GraphTraverseRequest(BaseModel):
    start_id:       str
    algorithm:      str             = "bfs"        # bfs | dfs
    depth:          int             = 2
    direction:      str             = "outbound"   # outbound | inbound | both
    relation_kinds: Optional[list[str]] = None     # None = all
    filters:        dict            = {}
    return_paths:   bool            = False
    limit:          int             = 100


class GraphPathRequest(BaseModel):
    start_id:  str
    end_id:    str
    max_depth: int = 6


class DistillRequest(BaseModel):
    content: str
    source:  str           = "api"
    model:   Optional[str] = None


class KeyRequest(BaseModel):
    label:  str       = "default"
    scopes: list[str] = ["read", "write"]


# Entity CRUD

@router.get("/{db}/raw/entities")
async def list_entities(
    db:          str = ...,
    status:      Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None, alias="type"),
    kind:        Optional[str] = Query(None),
    limit:       int = Query(50, le=500),
    cursor:      Optional[str] = Query(None),
    sections:    Optional[str] = Query(None, description="Comma-separated section names to include"),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)

    include_deleted = (status == "deleted")
    offset = decode_cursor(cursor) if cursor else 0

    # Filtering/paging touches the whole entity set — run it off the event loop
    # so a large database doesn't block other API clients.  Reads are served
    # from the in-memory cache, so this is fast once warm.
    def _work() -> tuple[list[dict], int]:
        entities = w.list_all(include_deleted=include_deleted)

        if status and status != "all":
            entities = [e for e in entities if e.get("status") == status]
        if entity_type:
            entities = [e for e in entities if e.get("type") == entity_type]
        if kind:
            def _kind_match(e: dict, k: str) -> bool:
                ek = e.get("kind")
                return k in ek if isinstance(ek, list) else ek == k
            entities = [e for e in entities if _kind_match(e, kind)]

        total = len(entities)
        page  = entities[offset : offset + limit]

        # Optionally project sections
        if sections:
            keys = {s.strip() for s in sections.split(",")}
            def _project(e):
                ec = dict(e)
                ec["sections"] = {k: v for k, v in e.get("sections", {}).items() if k in keys}
                return ec
            page = [_project(e) for e in page]

        # Always strip raw embedding float arrays from list responses.  Each
        # embedding can be 1,536+ floats; 50 entities × multiple models = MB of
        # useless data in a list view.  Use GET /{db}/raw/entities/{id} for vectors.
        page = [_strip_embeddings(e) for e in page]
        return page, total

    page, total = await asyncio.to_thread(_work)
    next_c = encode_cursor(offset + limit) if (offset + limit) < total else None

    return envelope(
        {"entities": page, "total": total, "returned": len(page), "offset": offset},
        db=db, took_start=t, cursor=next_c,
    )


def _lite(e: dict) -> dict:
    """Minimal row for the virtualized list view — just what a row renders."""
    sec = e.get("sections") or {}
    return {
        "id":              e.get("id"),
        "name":            e.get("name"),
        "type":            e.get("type"),
        "kind":            e.get("kind"),
        "status":          e.get("status"),
        "relations_count": len(sec.get("relations") or []),
    }


@router.get("/{db}/raw/entities/lite")
async def list_entities_lite(
    db:          str,
    status:      Optional[str] = Query("active"),
    entity_type: Optional[str] = Query(None, alias="type"),
    kind:        Optional[str] = Query(None),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Return *all* matching rows in a compact projection (id/name/type/kind/
    status/relations_count) for the virtualized explorer — no embeddings, no
    bodies, no pagination. Cheap enough to send tens of thousands of rows once
    and window them client-side."""
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)

    def _work() -> list[dict]:
        entities = w.list_all(include_deleted=(status == "deleted"))
        if status and status != "all":
            entities = [e for e in entities if e.get("status") == status]
        if entity_type:
            entities = [e for e in entities if e.get("type") == entity_type]
        if kind:
            def _km(e: dict) -> bool:
                ek = e.get("kind")
                return kind in ek if isinstance(ek, list) else ek == kind
            entities = [e for e in entities if _km(e)]
        rows = [_lite(e) for e in entities]
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows

    rows = await asyncio.to_thread(_work)
    return envelope({"rows": rows, "total": len(rows)}, db=db, took_start=t)


@router.get("/{db}/raw/entities/{entity_id}")
async def get_entity(
    db:        str,
    entity_id: str,
    sections:  Optional[str] = Query(None),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    entity = _writer(db).get(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    if sections:
        keys = {s.strip() for s in sections.split(",")}
        entity = {**entity, "sections": {k: v for k, v in entity.get("sections", {}).items() if k in keys}}

    return envelope(entity, db=db, took_start=t)


@router.post("/{db}/raw/entities/upsert")
async def upsert_entity(
    db:  str,
    req: UpsertRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
    x_actor:          Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    _ensure(db)

    w   = _writer(db)
    idx = _index(db)

    existing = w.get_by_name(req.name)

    if existing:
        # Build mutations dict — only non-None fields
        core_mut: dict = {}
        if req.summary    is not None: core_mut["summary"]    = req.summary
        if req.aliases    is not None: core_mut["aliases"]    = req.aliases
        if req.tags       is not None: core_mut["tags"]       = req.tags
        if req.categories is not None: core_mut["categories"] = req.categories

        mutations: dict = {}
        if req.type:   mutations["type"]   = req.type
        if req.source: mutations["source"] = req.source
        if req.kind   is not None: mutations["kind"]   = req.kind
        if req.status is not None: mutations["status"] = req.status
        if core_mut:   mutations.setdefault("sections", {})["core"] = core_mut
        if req.properties is not None:
            mutations.setdefault("sections", {})["properties"] = req.properties

        if req.relations is not None:
            rels = _resolve_relations(req.relations, w, idx)
            mutations.setdefault("sections", {})["relations"] = rels

        entity = w.update(existing["id"], mutations)
        action = "updated"
    else:
        # Create new entity
        sections_override: dict = {
            "core": {
                "summary":    req.summary    or "",
                "aliases":    req.aliases    or [],
                "tags":       req.tags       or [],
                "categories": req.categories or [],
            }
        }
        if req.properties:
            sections_override["properties"] = req.properties

        entity, _ = w.create(
            name=req.name,
            entity_type=req.type or "other",
            source=req.source or "api",
            kind=req.kind,
            status=req.status or "active",
            sections_override=sections_override,
        )

        if req.relations:
            rels = _resolve_relations(req.relations, w, idx)
            if rels:
                entity = w.update(entity["id"], {"sections": {"relations": rels}})

        action = "created"

    _emit(db, action, entity, x_actor)
    return envelope({"entity": entity, "action": action}, db=db, took_start=t)


def _resolve_relations(
    relations: list[RelationInput],
    w,
    idx,
) -> list[dict]:
    """Resolve target names → IDs. Creates stubs for unknown names."""
    result = []
    for r in relations:
        target_id = r.target_id
        if not target_id and r.target_name:
            target_id = idx.get(r.target_name)
            if not target_id:
                # Create a stub so the relation is not lost
                stub, _ = w.create(name=r.target_name, entity_type="other", source="stub")
                target_id = stub["id"]
        if target_id:
            result.append({"kind": r.kind, "target_id": target_id, "note": r.note})
    return result


class PatchRequest(BaseModel):
    mutations: dict[str, Any] = {}
    # Optional optimistic-concurrency guard: the version the client last read.
    # If the entity has since advanced, the patch is rejected with 409 instead
    # of clobbering the newer state. Can also be supplied via the If-Match header.
    expected_version: Optional[int] = None


def _parse_if_match(if_match: Optional[str]) -> Optional[int]:
    """Read an integer entity version out of an If-Match header.

    Accepts a bare number or a quoted ETag-style value (e.g. ``"3"`` or ``W/"3"``).
    Returns None when absent or unparseable, leaving the body field authoritative.
    """
    if not if_match:
        return None
    token = if_match.strip().lstrip("W/").strip().strip('"')
    try:
        return int(token)
    except ValueError:
        return None


@router.patch("/{db}/raw/entities/{entity_id}")
async def patch_entity(
    db:        str,
    entity_id: str,
    req:       PatchRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
    x_actor:          Optional[str] = Header(None),
    if_match:         Optional[str] = Header(None),
):
    from aethviondb.entity_writer import VersionConflictError

    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)
    if not w.exists(entity_id):
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    # Body field wins if both are given; otherwise fall back to the If-Match header.
    expected = req.expected_version
    if expected is None:
        expected = _parse_if_match(if_match)

    try:
        entity = w.update(entity_id, req.mutations, expected_version=expected)
    except VersionConflictError as e:
        raise HTTPException(
            409,
            detail={
                "error": "version_conflict",
                "message": str(e),
                "entity_id": e.entity_id,
                "expected_version": e.expected,
                "current_version": e.actual,
            },
        )
    _emit(db, "updated", entity, x_actor)
    return envelope({"entity": entity, "action": "patched"}, db=db, took_start=t)


@router.delete("/{db}/raw/entities/{entity_id}")
async def delete_entity(
    db:        str,
    entity_id: str,
    hard:      bool = Query(False),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
    x_actor:          Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)
    existing = w.get(entity_id)
    if not existing:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")
    w.delete(entity_id, soft=not hard)
    _emit(db, "deleted", existing, x_actor)
    return envelope(
        {"entity_id": entity_id, "mode": "hard" if hard else "soft"},
        db=db, took_start=t,
    )


@router.post("/{db}/raw/entities/batch")
async def batch_operations(
    db:  str,
    req: BatchRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
    x_actor:          Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    _ensure(db)

    w   = _writer(db)
    results: list[dict] = []
    errors:  list[dict] = []

    for i, op in enumerate(req.operations):
        try:
            if op.op == "upsert":
                data = op.data or {}
                name = data.get("name") or ""
                if not name:
                    raise ValueError("upsert requires 'name'")
                existing = w.get_by_name(name)
                if existing:
                    entity = w.update(existing["id"], data)
                    results.append({"index": i, "op": "upsert", "id": entity["id"], "action": "updated"})
                    _emit(db, "updated", entity, x_actor)
                else:
                    entity, _ = w.create(
                        name=name,
                        entity_type=data.get("type", "other"),
                        source=data.get("source", "api"),
                        sections_override={"core": {
                            "summary": data.get("summary", ""),
                            "tags":    data.get("tags", []),
                        }},
                    )
                    results.append({"index": i, "op": "upsert", "id": entity["id"], "action": "created"})
                    _emit(db, "created", entity, x_actor)

            elif op.op == "delete":
                if not op.id:
                    raise ValueError("delete requires 'id'")
                existing = w.get(op.id)
                if not existing:
                    raise ValueError(f"Entity '{op.id}' not found")
                w.delete(op.id, soft=not op.hard)
                results.append({"index": i, "op": "delete", "id": op.id})
                _emit(db, "deleted", existing, x_actor)

            elif op.op == "patch":
                if not op.id:
                    raise ValueError("patch requires 'id'")
                if not w.exists(op.id):
                    raise ValueError(f"Entity '{op.id}' not found")
                entity = w.update(op.id, op.mutations or {})
                results.append({"index": i, "op": "patch", "id": entity["id"]})
                _emit(db, "updated", entity, x_actor)

            else:
                raise ValueError(f"Unknown op {op.op!r}. Valid: upsert, delete, patch")

        except Exception as exc:
            errors.append({"index": i, "op": op.op, "error": str(exc)})
            if req.atomic:
                raise HTTPException(500, f"Batch aborted at op[{i}]: {exc}")

    return envelope(
        {
            "results":   results,
            "errors":    errors,
            "total":     len(req.operations),
            "succeeded": len(results),
            "failed":    len(errors),
        },
        db=db, took_start=t,
    )


# Entity sub-resources

@router.get("/{db}/raw/entities/{entity_id}/relations")
async def entity_relations(
    db:        str,
    entity_id: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w      = _writer(db)
    entity = w.get(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    # Build id→name map for relation resolution (names come from entity files, not index)
    id_to_name = {e["id"]: e["name"] for e in w.list_all(include_deleted=True)}

    rels     = (entity.get("sections") or {}).get("relations", [])
    enriched = []
    for r in rels:
        tid = r.get("target_id", "")
        enriched.append({**r, "target_name": id_to_name.get(tid, tid)})

    return envelope(
        {"entity_id": entity_id, "relations": enriched, "count": len(enriched)},
        db=db, took_start=t,
    )


@router.get("/{db}/raw/entities/{entity_id}/vectors")
async def entity_vectors(
    db:        str,
    entity_id: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    entity = _writer(db).get(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    vecs = (entity.get("sections") or {}).get("vectors", {})
    summary = {
        model: {
            "dimensions":  data.get("dimensions") or (len(data.get("embedding", [])) if isinstance(data, dict) else 0),
            "model":       data.get("model", model) if isinstance(data, dict) else model,
            "embedded_at": data.get("embedded_at") if isinstance(data, dict) else None,
        }
        for model, data in vecs.items()
    }
    return envelope(
        {"entity_id": entity_id, "vector_models": list(vecs.keys()), "summary": summary},
        db=db, took_start=t,
    )


@router.get("/{db}/raw/entities/{entity_id}/timeline")
async def entity_timeline(
    db:        str,
    entity_id: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    entity = _writer(db).get(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    timeline = (entity.get("sections") or {}).get("timeline", [])
    return envelope(
        {"entity_id": entity_id, "timeline": timeline, "count": len(timeline)},
        db=db, took_start=t,
    )


# Hybrid Search

@router.post("/{db}/raw/search")
async def hybrid_search(
    db:  str,
    req: HybridSearchRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)

    w        = _writer(db)
    entities = w.list_all()
    entities = _apply_filters(entities, req.filters)

    offset = decode_cursor(req.cursor) if req.cursor else 0

    # Keyword scoring
    do_keyword = "keyword" in req.modes
    do_vector  = "vector"  in req.modes and req.vector_model

    query_vec: list[float] | None = None
    if do_vector and req.query:
        try:
            from aethviondb.vectorizer import _embed
            query_vec = await asyncio.to_thread(_embed, req.query, req.vector_model)
        except Exception as exc:
            logger.warning(f"[API v1] Vector embed failed in hybrid search: {exc}")
            do_vector = False

    scored: list[dict] = []
    for e in entities:
        kw_score  = _keyword_score(req.query, e) if do_keyword else 0.0
        vec_score = 0.0
        if do_vector and query_vec:
            vecs     = (e.get("sections") or {}).get("vectors", {})
            emb_data = vecs.get(req.vector_model)
            if isinstance(emb_data, dict):
                emb = emb_data.get("embedding") or emb_data.get("vector")
                if emb:
                    vec_score = _cosine(query_vec, emb)

        # Combine: if both active, weight 60% vector / 40% keyword
        if do_keyword and do_vector:
            score = 0.4 * kw_score + 0.6 * vec_score
        elif do_vector:
            score = vec_score
        else:
            score = kw_score

        if score >= req.min_score or (not req.query and not do_vector):
            row = _flat_summary(e)
            row["score"] = round(score, 4)
            if do_keyword and do_vector:
                row["score_breakdown"] = {
                    "keyword": round(kw_score,  4),
                    "vector":  round(vec_score, 4),
                }
            scored.append(row)

    scored.sort(key=lambda r: r.get("score", 0), reverse=True)
    total  = len(scored)
    page   = scored[offset : offset + req.limit]
    next_c = encode_cursor(offset + req.limit) if (offset + req.limit) < total else None

    return envelope(
        {
            "results":            page,
            "total_matched":      total,
            "total_scanned":      len(entities),
            "vector_search_used": bool(do_vector and query_vec),
        },
        db=db, took_start=t, cursor=next_c,
    )


# Vector Similarity Search

@router.post("/{db}/raw/vectors/search")
async def vector_similarity_search(
    db:  str,
    req: VectorSearchRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)

    if not req.query and not req.vector:
        raise HTTPException(400, "Provide either 'query' (text) or 'vector' (pre-embedded floats).")

    query_vec = req.vector
    if not query_vec and req.query:
        try:
            from aethviondb.vectorizer import _embed
            # _embed is already async (uses asyncio.to_thread internally for I/O);
            # do NOT wrap it in to_thread again — that would run the coroutine
            # constructor in a thread, return an unawaited coroutine object, and
            # cause a TypeError when the cosine function tries to iterate over it.
            query_vec = await _embed(req.query, req.model)
        except Exception as exc:
            raise HTTPException(500, f"Embedding failed: {exc}")

    w        = _writer(db)
    entities = _apply_filters(w.list_all(), req.filters)

    results: list[dict] = []
    for e in entities:
        vecs     = (e.get("sections") or {}).get("vectors", {})
        emb_data = vecs.get(req.model)
        if not isinstance(emb_data, dict):
            continue
        emb = emb_data.get("embedding") or emb_data.get("vector")
        if not emb:
            continue
        score = _cosine(query_vec, emb)
        results.append({**_flat_summary(e), "score": round(score, 4)})

    results.sort(key=lambda r: r["score"], reverse=True)
    return envelope(
        {"results": results[: req.top_k], "model": req.model, "top_k": req.top_k},
        db=db, took_start=t,
    )


# Embedding generation (vectorize)

class VectorizeRequest(BaseModel):
    model:         str  = "all-MiniLM-L6-v2"
    force_rewrite: bool = False
    include_stubs: bool = True


@router.get("/{db}/raw/vectorize/models")
async def vectorize_models(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """List the embedding models available for generation (local + API providers)."""
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    from aethviondb.vectorizer import EMBEDDING_MODELS, EMBEDDING_COSTS
    models = [
        {
            "model":       name,
            "provider":    info.get("provider"),
            "dimensions":  info.get("dimensions"),
            "description": info.get("description", ""),
            "cost_per_1m": EMBEDDING_COSTS.get(name, 0.0),
        }
        for name, info in EMBEDDING_MODELS.items()
    ]
    return envelope({"models": models}, db=db, took_start=t)


@router.get("/{db}/raw/vectorize/status")
async def vectorize_status(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Current vectorization progress (reads the VECINFO sidecar)."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb.vectorizer import read_vec_info, is_vectorizing
    info = read_vec_info(root) or {}
    return envelope({"running": is_vectorizing(root), "info": info}, db=db, took_start=t)


@router.post("/{db}/raw/vectorize")
async def start_vectorize(
    db:  str,
    req: VectorizeRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Start a background pass that embeds every non-deleted entity and stores
    the vector on the raw entity (sections.vectors). Progress via /vectorize/status."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    _ensure(db)

    from aethviondb.vectorizer import (
        vectorize_all, is_vectorizing, _vec_tasks, EMBEDDING_MODELS,
    )

    if is_vectorizing(root):
        raise HTTPException(409, "A vectorization pass is already running for this database.")
    if req.model not in EMBEDDING_MODELS:
        raise HTTPException(400, f"Unknown model {req.model!r}. See /vectorize/models.")

    w    = _writer(db)
    key  = str(root)
    task = asyncio.create_task(
        vectorize_all(
            root, w, req.model,
            force_rewrite=req.force_rewrite,
            include_stubs=req.include_stubs,
        )
    )
    _vec_tasks[key] = task
    task.add_done_callback(lambda _: _vec_tasks.pop(key, None))

    return envelope({"started": True, "model": req.model}, db=db, took_start=t)


@router.post("/{db}/raw/vectorize/cancel")
async def cancel_vectorize_endpoint(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Cancel a running vectorization pass."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb.vectorizer import cancel_vectorize
    return envelope(cancel_vectorize(root), db=db, took_start=t)


# Snapshot export (round-trips with the .snapshot importer)

@router.get("/{db}/raw/snapshot/download")
async def download_snapshot(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Export the database as a portable AethvionDB ``.snapshot`` file — a JSON
    array of every entity, importable via the snapshot importer."""
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb import snapshot as snap

    w = _writer(db)
    # Build a fresh snapshot at the current generation so the download is current
    # (includes deleted entities for a faithful, complete round-trip).
    entities = await asyncio.to_thread(lambda: w.list_all(include_deleted=True))
    await asyncio.to_thread(snap.build, root, entities)

    path = snap.snapshot_path(root)
    if not path.exists():
        raise HTTPException(404, "Snapshot could not be built.")
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{db}.snapshot",
    )


# Live change feed (Server-Sent Events) — lets agents and the dashboard see
# every write the moment it happens.

@router.get("/{db}/events")
async def events_stream(
    db:      str,
    request: Request,
    key:     Optional[str] = Query(None, description="API key (EventSource can't send headers)"),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Stream change events for this database as Server-Sent Events.

    Each event is one JSON line: action, id, name, type, version, actor, ts.
    The browser EventSource API can't set headers, so a key may be passed as a
    ``?key=`` query parameter for protected databases.
    """
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key or key)

    from aethviondb import events
    queue, unsubscribe = events.subscribe(db)

    async def stream():
        try:
            # Open the stream and tell the client how often we heartbeat.
            yield "retry: 3000\n: connected\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"          # heartbeat keeps proxies/clients alive
                if await request.is_disconnected():
                    break
        finally:
            unsubscribe()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# Backups — point-in-time copies of a database, with restore.

class BackupCreate(BaseModel):
    label: str = ""


@router.get("/{db}/backups")
async def list_backups_endpoint(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """List point-in-time backups of this database, newest first."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb.backup import list_backups
    backups = await asyncio.to_thread(list_backups, root)
    return envelope({"backups": backups, "total": len(backups)}, db=db, took_start=t)


@router.post("/{db}/backups")
async def create_backup_endpoint(
    db:  str,
    req: BackupCreate,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Create a point-in-time backup (copies entities + name index)."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    _ensure(db)
    from aethviondb.backup import create_backup
    try:
        meta = await asyncio.to_thread(create_backup, root, db, req.label)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return envelope(meta, db=db, took_start=t)


@router.post("/{db}/backups/{backup_id}/restore")
async def restore_backup_endpoint(
    db:        str,
    backup_id: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Restore a backup, replacing the current database contents. The in-memory
    and on-disk caches are invalidated so the next read reflects the restore."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb.backup import restore_backup
    try:
        report = await asyncio.to_thread(restore_backup, root, backup_id)
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return envelope(report, db=db, took_start=t)


@router.delete("/{db}/backups/{backup_id}")
async def delete_backup_endpoint(
    db:        str,
    backup_id: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Delete a backup."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from aethviondb.backup import delete_backup
    if not await asyncio.to_thread(delete_backup, root, backup_id):
        raise HTTPException(404, f"Backup {backup_id!r} not found.")
    return envelope({"deleted": backup_id}, db=db, took_start=t)


# Validation / health — cross-entity consistency report.

@router.get("/{db}/raw/validate")
async def validate_database(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    """Run the consistency checks across the whole database and return an
    aggregate health report: totals, entities with errors, duplicate name/alias
    groups, a warning breakdown, orphan stubs, and soft-deleted files pending
    purge. Runs off the event loop (it touches every entity)."""
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    w = _writer(db)
    from aethviondb.validator import Validator
    report = await asyncio.to_thread(lambda: Validator(writer=w).summary())
    return envelope(report, db=db, took_start=t)


# Graph

def _bfs(writer, start_id: str, depth: int, direction: str,
         relation_kinds: list[str] | None, filters: dict) -> dict[str, list[str]]:
    """BFS from start_id. Returns {node_id: [path ids]}."""
    visited: dict[str, list[str]] = {start_id: [start_id]}
    frontier = [start_id]
    for _ in range(depth):
        nxt: list[str] = []
        for eid in frontier:
            entity = writer.get(eid)
            if not entity:
                continue
            rels = (entity.get("sections") or {}).get("relations", [])
            for r in rels:
                if relation_kinds and r.get("kind") not in relation_kinds:
                    continue
                if direction in ("outbound", "both"):
                    tid = r.get("target_id")
                    if tid and tid not in visited:
                        visited[tid] = visited[eid] + [tid]
                        nxt.append(tid)
            # Inbound: scan all for relations pointing to this node (expensive for large DBs)
            if direction in ("inbound", "both"):
                for e2 in writer.list_all():
                    for r in (e2.get("sections") or {}).get("relations", []):
                        if r.get("target_id") == eid and e2["id"] not in visited:
                            visited[e2["id"]] = visited[eid] + [e2["id"]]
                            nxt.append(e2["id"])
        frontier = nxt
        if not frontier:
            break
    return visited


@router.post("/{db}/raw/graph/traverse")
async def graph_traverse(
    db:  str,
    req: GraphTraverseRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)

    if not w.exists(req.start_id):
        raise HTTPException(404, f"Start entity '{req.start_id}' not found.")

    paths_map = await asyncio.to_thread(
        _bfs, w, req.start_id, req.depth, req.direction,
        req.relation_kinds, req.filters,
    )
    included = set(paths_map.keys())
    all_e    = {e["id"]: e for e in w.list_all()}

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_edges: set   = set()

    for eid in list(included)[: req.limit]:
        e = all_e.get(eid)
        if not e:
            continue
        if req.filters:
            if not _apply_filters([e], req.filters):
                continue
        row = _flat_summary(e)
        row["depth"] = len(paths_map[eid]) - 1
        if req.return_paths:
            row["path"] = paths_map[eid]
        nodes.append(row)

        for r in (e.get("sections") or {}).get("relations", []):
            tid = r.get("target_id")
            if tid not in included:
                continue
            if req.relation_kinds and r.get("kind") not in req.relation_kinds:
                continue
            key = (eid, tid, r.get("kind", ""))
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append({"source": eid, "target": tid, "kind": r.get("kind", "related_to")})

    start_entity = all_e.get(req.start_id)
    return envelope(
        {
            "root":       {"id": req.start_id, "name": start_entity.get("name") if start_entity else req.start_id},
            "nodes":      nodes,
            "edges":      edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        db=db, took_start=t,
    )


@router.get("/{db}/raw/graph/neighbors/{entity_id}")
async def graph_neighbors(
    db:        str,
    entity_id: str,
    direction: str = Query("both"),
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)
    entity = w.get(entity_id)
    if not entity:
        raise HTTPException(404, f"Entity '{entity_id}' not found.")

    all_e    = {e["id"]: e for e in w.list_all()}
    outbound = []
    inbound  = []

    if direction in ("outbound", "both"):
        for r in (entity.get("sections") or {}).get("relations", []):
            tid = r.get("target_id", "")
            te  = all_e.get(tid)
            outbound.append({
                "id":   tid,
                "name": te["name"] if te else tid,
                "kind": r.get("kind"),
                "note": r.get("note", ""),
            })

    if direction in ("inbound", "both"):
        for e in all_e.values():
            for r in (e.get("sections") or {}).get("relations", []):
                if r.get("target_id") == entity_id:
                    inbound.append({
                        "id":   e["id"],
                        "name": e["name"],
                        "kind": r.get("kind"),
                        "note": r.get("note", ""),
                    })

    return envelope(
        {
            "entity_id": entity_id,
            "outbound":  outbound,
            "inbound":   inbound,
            "total":     len(outbound) + len(inbound),
        },
        db=db, took_start=t,
    )


@router.post("/{db}/raw/graph/path")
async def graph_path(
    db:  str,
    req: GraphPathRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    check_auth(_root(db), authorization, x_aethviondb_key)
    w = _writer(db)

    for eid, label in [(req.start_id, "start"), (req.end_id, "end")]:
        if not w.exists(eid):
            raise HTTPException(404, f"{label.capitalize()} entity '{eid}' not found.")

    from collections import deque
    queue:   deque = deque([(req.start_id, [req.start_id])])
    visited: set   = {req.start_id}

    path: list[str] | None = None
    while queue:
        node, current_path = queue.popleft()
        if len(current_path) > req.max_depth + 1:
            break
        entity = w.get(node)
        if not entity:
            continue
        for r in (entity.get("sections") or {}).get("relations", []):
            tid = r.get("target_id")
            if not tid:
                continue
            if tid == req.end_id:
                path = current_path + [tid]
                break
            if tid not in visited:
                visited.add(tid)
                queue.append((tid, current_path + [tid]))
        if path:
            break

    all_e = {e["id"]: e for e in w.list_all()}
    if path:
        nodes = [{"id": nid, "name": all_e[nid]["name"] if nid in all_e else nid} for nid in path]
    else:
        nodes = []

    return envelope(
        {
            "found":  path is not None,
            "length": len(path) - 1 if path else None,
            "path":   path or [],
            "nodes":  nodes,
        },
        db=db, took_start=t,
    )


# AI Distillation

@router.post("/{db}/raw/distill")
async def distill_text(
    db:  str,
    req: DistillRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    _ensure(db)

    from aethviondb.distiller import ContentDistiller
    w   = _writer(db)
    idx = _index(db)
    d   = ContentDistiller(writer=w, index=idx)
    result = await d.distill(content=req.content, model=req.model, source=req.source)

    if result.get("errors"):
        raise HTTPException(500, {"code": "DISTILL_ERROR", "errors": result["errors"]})

    return envelope(result, db=db, took_start=t)


# API Key management

@router.get("/{db}/keys")
async def list_keys_endpoint(
    db: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from .auth import list_keys
    return envelope({"keys": list_keys(root), "open_access": not bool(list_keys(root))}, db=db, took_start=t)


@router.post("/{db}/keys")
async def generate_key_endpoint(
    db:  str,
    req: KeyRequest,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    # Note: allow unauthenticated when generating the very first key
    from .auth import has_keys
    if has_keys(root):
        check_auth(root, authorization, x_aethviondb_key)
    from .auth import generate_key
    try:
        raw_key = generate_key(root, label=req.label, scopes=req.scopes)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return envelope(
        {
            "key":     raw_key,
            "label":   req.label,
            "scopes":  req.scopes,
            "warning": "This is the only time this key will be shown. Copy it now.",
        },
        db=db, took_start=t,
    )


@router.delete("/{db}/keys/{label}")
async def revoke_key_endpoint(
    db:    str,
    label: str,
    authorization:    Optional[str] = Header(None),
    x_aethviondb_key: Optional[str] = Header(None),
):
    t = time.perf_counter()
    root = _root(db)
    check_auth(root, authorization, x_aethviondb_key)
    from .auth import revoke_key
    if not revoke_key(root, label):
        raise HTTPException(404, f"No key with label {label!r} found.")
    return envelope({"revoked": label}, db=db, took_start=t)
