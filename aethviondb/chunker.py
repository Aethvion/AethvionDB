"""
core/aethviondb/chunker.py
Smart entity chunking for AethvionDB.

Chunks are logical groupings stored as an index layer on top of raw entity
files.  The individual files are never modified.

Auto-detection strategy (applied in order):
  1. Type      — group by entity type (person, element, concept, …)
  2. Property  — within each type, sub-group by the most-shared property key
  3. Tag       — if no dominant property, sub-group by most-frequent tags
  4. Alpha     — if > ALPHA_THRESH entities with no sub-grouping: A–M / N–Z

Output layout
  db_root/chunks/manifest.json   — chunk list + metadata (no raw index data)
  db_root/chunks/{chunk_id}.json — entity IDs + inverted term index per chunk

Inverted index
Each chunk file stores a term → [entity_id, …] mapping built from:
  name, aliases, summary (first 300 chars), tags, categories, type, top
  property values.

Search uses term-coverage scoring: matched_tokens / total_query_tokens.
BM25-like IDF boost can be layered on later; for most knowledge DBs the
simple coverage score produces excellent results.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aethviondb._utils import get_logger

if TYPE_CHECKING:
    from .entity_writer import EntityWriter

logger = get_logger(__name__)

# Constants

CHUNKS_DIR    = "chunks"
MANIFEST_FILE = "manifest.json"
CHUNK_PREFIX  = "chunk_"

MIN_SUBGROUP  = 4     # minimum entities to justify a sub-group
PROP_THRESH   = 0.55  # property key must be present in ≥55 % of entities
TAG_MIN_COUNT = 3     # tag must appear ≥3 times to anchor a sub-group
ALPHA_THRESH  = 25    # split A–M / N–Z when a type has > ALPHA_THRESH entities

_STOPWORDS = frozenset(
    "a an the and or but in of to is are was were be been being have has had "
    "do does did will would could should may might must can shall not no nor "
    "for with at by from this that these those it its we they you he she i am "
    "on into as out up about over after before between through during against "
    "also just more most some any all both each few other same so than then "
    "very own like when where how what which who only if".split()
)


# Tokenisation helpers

def _tokenize(text: str) -> list[str]:
    """Lowercase → split on non-alphanumeric → filter stopwords & shorts."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]


def _entity_tokens(entity: dict) -> list[str]:
    """Extract every searchable token from one entity (no deduplication)."""
    sec  = entity.get("sections", {})
    core = sec.get("core", {})

    tokens: list[str] = []
    tokens += _tokenize(entity.get("name", ""))
    for alias in core.get("aliases", []):
        tokens += _tokenize(alias)
    tokens += _tokenize(core.get("summary", "")[:300])
    for tag in core.get("tags", []):
        tokens += _tokenize(tag)
    for cat in core.get("categories", []):
        tokens += _tokenize(cat)
    # Entity type as a searchable token ("find all element entities")
    tokens += _tokenize(entity.get("type", ""))
    # Top 5 property values
    for _k, v in list(sec.get("properties", {}).items())[:5]:
        tokens += _tokenize(str(v))
    return tokens


# Chunk ID

def _chunk_id(label: str) -> str:
    """Deterministic 12-char ID for a chunk label."""
    return CHUNK_PREFIX + hashlib.sha1(label.encode()).hexdigest()[:12]


# ISO timestamp

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Inverted index builder

def _build_index(entities: list[dict]) -> dict[str, list[str]]:
    """Build inverted index: term → sorted unique entity IDs."""
    index: dict[str, set[str]] = defaultdict(set)
    for entity in entities:
        eid = entity["id"]
        for token in set(_entity_tokens(entity)):   # per-entity dedup
            index[token].add(eid)
    return {term: sorted(ids) for term, ids in sorted(index.items())}


# Auto-chunking algorithm

def _auto_chunk(entities: list[dict]) -> list[dict]:
    """
    Produce raw chunk descriptors from a flat entity list.

    Returns:
        list of {label, entity_ids, type, strategy, group_key, group_val}
    """
    # Step 1: group by entity type
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in entities:
        by_type[e.get("type", "other")].append(e)

    chunks: list[dict] = []

    for type_name, type_ents in sorted(by_type.items()):

        if len(type_ents) < MIN_SUBGROUP:
            # Too few entities — single flat chunk
            chunks.append({
                "label":      type_name,
                "entity_ids": [e["id"] for e in type_ents],
                "type":       type_name,
                "strategy":   "type",
                "group_key":  None,
                "group_val":  None,
            })
            continue

        # Step 2: find dominant property key
        prop_counts: Counter = Counter()
        for e in type_ents:
            props = (e.get("sections") or {}).get("properties", {})
            for k in props:
                prop_counts[k] += 1

        dominant_key: str | None = None
        for key, count in prop_counts.most_common(5):
            if count < len(type_ents) * PROP_THRESH:
                break
            # How many distinct values does this key have?
            vals = {
                str((e.get("sections") or {}).get("properties", {}).get(key, ""))
                for e in type_ents
            }
            if len(vals) > 1:
                dominant_key = key
                break

        if dominant_key:
            by_val: dict[str, list[dict]] = defaultdict(list)
            for e in type_ents:
                val = str(
                    (e.get("sections") or {}).get("properties", {}).get(dominant_key, "other")
                )
                by_val[val].append(e)

            for val, val_ents in sorted(by_val.items()):
                label = f"{type_name} — {dominant_key}: {val}"
                chunks.append({
                    "label":      label,
                    "entity_ids": [e["id"] for e in val_ents],
                    "type":       type_name,
                    "strategy":   "property",
                    "group_key":  dominant_key,
                    "group_val":  val,
                })
            continue

        # Step 3: sub-group by most-frequent tags
        tag_counts: Counter = Counter()
        for e in type_ents:
            for tag in (e.get("sections") or {}).get("core", {}).get("tags", []):
                tag_counts[tag] += 1

        top_tags = [t for t, c in tag_counts.most_common(10) if c >= TAG_MIN_COUNT]

        if len(top_tags) >= 2:
            by_tag: dict[str, list[dict]] = defaultdict(list)
            assigned: set[str] = set()
            for e in type_ents:
                e_tags = set((e.get("sections") or {}).get("core", {}).get("tags", []))
                for tag in top_tags:
                    if tag in e_tags:
                        by_tag[tag].append(e)
                        assigned.add(e["id"])
                        break

            other_ents = [e for e in type_ents if e["id"] not in assigned]

            for tag, tag_ents in sorted(by_tag.items()):
                chunks.append({
                    "label":      f"{type_name} — {tag}",
                    "entity_ids": [e["id"] for e in tag_ents],
                    "type":       type_name,
                    "strategy":   "tag",
                    "group_key":  "tag",
                    "group_val":  tag,
                })
            if other_ents:
                chunks.append({
                    "label":      f"{type_name} — other",
                    "entity_ids": [e["id"] for e in other_ents],
                    "type":       type_name,
                    "strategy":   "tag",
                    "group_key":  "tag",
                    "group_val":  "other",
                })
            continue

        # Step 4: alphabetical split for large type groups
        if len(type_ents) > ALPHA_THRESH:
            first  = [e for e in type_ents if e.get("name", "Z")[0].upper() < "N"]
            second = [e for e in type_ents if e.get("name", "Z")[0].upper() >= "N"]
            if first:
                chunks.append({
                    "label":      f"{type_name} — A–M",
                    "entity_ids": [e["id"] for e in first],
                    "type":       type_name,
                    "strategy":   "alpha",
                    "group_key":  "name",
                    "group_val":  "A-M",
                })
            if second:
                chunks.append({
                    "label":      f"{type_name} — N–Z",
                    "entity_ids": [e["id"] for e in second],
                    "type":       type_name,
                    "strategy":   "alpha",
                    "group_key":  "name",
                    "group_val":  "N-Z",
                })
        else:
            chunks.append({
                "label":      type_name,
                "entity_ids": [e["id"] for e in type_ents],
                "type":       type_name,
                "strategy":   "type",
                "group_key":  None,
                "group_val":  None,
            })

    return chunks


# Path helpers

def chunk_dir(db_root: Path) -> Path:
    return db_root / CHUNKS_DIR


def manifest_path(db_root: Path) -> Path:
    return chunk_dir(db_root) / MANIFEST_FILE


def chunk_file_path(db_root: Path, chunk_id: str) -> Path:
    return chunk_dir(db_root) / f"{chunk_id}.json"


# Public API

def read_manifest(db_root: Path) -> dict | None:
    """Return the chunk manifest dict, or None if it doesn't exist / is corrupt."""
    p = manifest_path(db_root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def build_chunks(db_root: Path, writer: "EntityWriter") -> dict:
    """
    Build (or rebuild) the chunk manifest + per-chunk index files.

    Runs synchronously — call via ``asyncio.to_thread`` for large databases.
    Stale chunk files are automatically removed.
    Returns the manifest dict.
    """
    t0 = time.perf_counter()
    chunk_dir(db_root).mkdir(parents=True, exist_ok=True)

    all_entities = writer.list_all(include_deleted=False)
    entity_map   = {e["id"]: e for e in all_entities}

    raw_chunks = _auto_chunk(all_entities)

    chunk_meta: list[dict] = []
    for rc in raw_chunks:
        cid   = _chunk_id(rc["label"])
        ids   = rc["entity_ids"]
        ents  = [entity_map[i] for i in ids if i in entity_map]
        index = _build_index(ents)

        chunk_file_path(db_root, cid).write_text(
            json.dumps({
                "id":           cid,
                "label":        rc["label"],
                "type":         rc["type"],
                "strategy":     rc["strategy"],
                "group_key":    rc["group_key"],
                "group_val":    rc["group_val"],
                "entity_count": len(ids),
                "entity_ids":   ids,
                "index":        index,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        chunk_meta.append({
            "id":           cid,
            "label":        rc["label"],
            "type":         rc["type"],
            "strategy":     rc["strategy"],
            "group_key":    rc["group_key"],
            "group_val":    rc["group_val"],
            "entity_count": len(ids),
        })

    elapsed = round((time.perf_counter() - t0) * 1000, 1)

    # Remove stale chunk files from previous builds
    valid_filenames = {f"{m['id']}.json" for m in chunk_meta} | {MANIFEST_FILE}
    for f in chunk_dir(db_root).glob("*.json"):
        if f.name not in valid_filenames:
            try:
                f.unlink()
            except Exception:
                pass

    manifest = {
        "built_at":     _now_iso(),
        "entity_count": len(all_entities),
        "chunk_count":  len(raw_chunks),
        "elapsed_ms":   elapsed,
        "chunks":       chunk_meta,
    }
    manifest_path(db_root).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        f"[Chunker] Built {len(raw_chunks)} chunks "
        f"for {len(all_entities)} entities in {elapsed} ms"
    )
    return manifest


def get_chunk(db_root: Path, chunk_id: str) -> dict | None:
    """Load a single chunk file (includes the inverted index)."""
    p = chunk_file_path(db_root, chunk_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def search_chunks(
    db_root:  Path,
    query:    str,
    chunk_id: str | None = None,
    top_k:    int        = 20,
) -> list[dict]:
    """
    BM25-inspired search across one or all chunk indices.

    Parameters
    ----------
    db_root  : database root path
    query    : free-text search query
    chunk_id : restrict search to this chunk when given
    top_k    : maximum results to return

    Returns
    -------
    list of {entity_id, chunk_id, chunk_label, score, hits} sorted by score desc.
    """
    query_tokens = list(set(_tokenize(query)))
    if not query_tokens:
        return []

    # Choose which chunk files to scan
    if chunk_id:
        chunk_files = [chunk_file_path(db_root, chunk_id)]
    else:
        manifest = read_manifest(db_root)
        if not manifest:
            return []
        chunk_files = [
            chunk_file_path(db_root, c["id"])
            for c in manifest["chunks"]
        ]

    # entity_id → best result dict
    results: dict[str, dict] = {}

    for cf in chunk_files:
        if not cf.exists():
            continue
        try:
            chunk = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            continue

        cid    = chunk["id"]
        clabel = chunk.get("label", cid)
        index  = chunk.get("index", {})

        entity_hits: Counter = Counter()
        for token in query_tokens:
            for eid in index.get(token, []):
                entity_hits[eid] += 1

        for eid, hits in entity_hits.items():
            score = hits / len(query_tokens)
            if eid not in results or score > results[eid]["score"]:
                results[eid] = {
                    "entity_id":   eid,
                    "chunk_id":    cid,
                    "chunk_label": clabel,
                    "score":       round(score, 3),
                    "hits":        hits,
                }

    return sorted(results.values(), key=lambda r: r["score"], reverse=True)[:top_k]
