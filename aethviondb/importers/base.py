"""
aethviondb/importers/base.py
Framework for importing external databases into AethvionDB (a Layer-2 concern).

Each source type (SQLite, CSV, Pinecone, …) is a small adapter that knows how to
read its source and yield AethvionDB entities. This framework owns the shared,
source-agnostic part: building valid entities and writing them in bulk. Adapters
never touch the Layer-1 core — they just produce entities for it to store.
"""
from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from aethviondb import snapshot
from aethviondb._utils import atomic_json_write, get_logger
from aethviondb.entity_schema import make_empty

logger = get_logger(__name__)


def deterministic_id(*parts: Any) -> str:
    """Stable entity ID from source-identifying parts (e.g. table + primary key).

    Deterministic so (a) re-importing the same row overwrites instead of
    duplicating, and (b) a foreign key can reference a target row's ID without
    that row having been written yet.
    """
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return "ws_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def make_entity(*, entity_id: str, name: str, kind: str, source: str,
                summary: str = "", tags: list | None = None,
                properties: dict | None = None, relations: list | None = None,
                entity_type: str = "other") -> dict:
    """Build a valid AethvionDB entity for an imported record."""
    e = make_empty(name=name, entity_type=entity_type, source=source,
                   entity_id=entity_id, kind=kind)
    e["sections"]["core"]["summary"] = summary or ""
    if tags:
        e["sections"]["core"]["tags"] = list(tags)
    if properties:
        e["sections"]["properties"] = properties
    if relations:
        e["sections"]["relations"] = relations
    return e


def bulk_write(db_root: Path, entities: list[dict]) -> int:
    """Write many entities, then rebuild the snapshot once.

    Avoids the per-write index/snapshot cost (the file-syscall-bound path the
    benchmark flagged) — the right approach for an import. Rebuilds the snapshot
    from every entity file on disk, so it is correct whether the target database
    is empty or already has entities. Returns the number written.
    """
    edir = db_root / "entities"
    edir.mkdir(parents=True, exist_ok=True)
    for e in entities:
        atomic_json_write(edir / f"{e['id']}.json", e)

    all_entities: list[dict] = []
    for p in edir.glob("ws_*.json"):
        try:
            all_entities.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("[import] could not read %s: %s", p, exc)

    snapshot.invalidate(db_root)            # drop stale cache + bump generation
    snapshot.build(db_root, all_entities)   # one rebuild for the whole import
    return len(entities)


@dataclass
class ImportSummary:
    source_type: str
    db: str
    entities: int = 0
    relations: int = 0
    by_kind: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    warnings: list = field(default_factory=list)


class BaseImporter(ABC):
    """One adapter per source type. Subclasses implement preview + iter_entities."""

    source_type: str = "base"
    # File extensions this adapter recognises, used when scanning a folder.
    extensions: tuple[str, ...] = ()

    def __init__(self, source: str):
        self.source = source

    @abstractmethod
    def preview(self) -> dict:
        """Describe what would be imported, WITHOUT writing anything."""

    @abstractmethod
    def iter_entities(self) -> Iterator[dict]:
        """Yield AethvionDB entity dicts (relations reference target IDs)."""

    def run(self, db_root: Path, db_name: str) -> ImportSummary:
        t0 = time.perf_counter()
        entities = list(self.iter_entities())
        relations = sum(len(e["sections"].get("relations", [])) for e in entities)
        by_kind: dict[str, int] = {}
        for e in entities:
            k = e.get("kind") or "other"
            by_kind[k] = by_kind.get(k, 0) + 1
        bulk_write(db_root, entities)
        return ImportSummary(
            source_type=self.source_type, db=db_name,
            entities=len(entities), relations=relations, by_kind=by_kind,
            elapsed_s=round(time.perf_counter() - t0, 3),
        )
