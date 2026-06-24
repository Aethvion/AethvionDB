"""
aethviondb/importers/snapshot.py
Import an AethvionDB snapshot — a `.snapshot` file.

This is the round-trip partner of the on-disk snapshot the engine writes
(``AethvionDB.SNAPSHOT``): a compact JSON array of full AethvionDB entities.
Because the records are already in AethvionDB's envelope, the import is a
faithful copy — IDs, versions, sections, relations and timestamps are preserved
exactly. Importing into an existing database merges by ID (same ID overwrites).

The name index is rebuilt from the imported entities so name lookups and
get-or-create dedup work immediately after a round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from aethviondb._utils import get_logger
from .base import BaseImporter, ImportSummary

logger = get_logger(__name__)


class SnapshotImporter(BaseImporter):
    source_type = "snapshot"
    extensions = (".snapshot",)

    def __init__(self, source: str):
        super().__init__(source)
        self.path = Path(source)
        if not self.path.exists():
            raise FileNotFoundError(f"No file at: {source}")
        if not self.path.is_file():
            raise ValueError(
                f"That path is a folder, not a snapshot file. Point at a "
                f".snapshot file inside it."
            )
        # Cheap header sniff (don't parse the whole file just to validate, so a
        # folder scan stays fast): an AethvionDB snapshot is a JSON array.
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                head = f.read(64).lstrip()
        except OSError as e:
            raise ValueError(f"Could not read file: {e}")
        if not head.startswith("["):
            raise ValueError(
                f"Not an AethvionDB snapshot (expected a JSON array): {self.path.name}"
            )
        self._data: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"Snapshot is not valid JSON: {e}")
            if not isinstance(data, list):
                raise ValueError("Snapshot must be a JSON array of entities.")
            if data and not (isinstance(data[0], dict) and "id" in data[0] and "sections" in data[0]):
                raise ValueError(
                    "File does not look like an AethvionDB snapshot "
                    "(entities need 'id' and 'sections')."
                )
            self._data = data
        return self._data

    def preview(self) -> dict:
        data = self._load()
        by_kind: dict[str, int] = {}
        by_type: dict[str, int] = {}
        rels = 0
        for e in data:
            k = e.get("kind") or "—"
            if isinstance(k, list):
                k = k[0] if k else "—"
            by_kind[k] = by_kind.get(k, 0) + 1
            t = e.get("type") or "other"
            by_type[t] = by_type.get(t, 0) + 1
            rels += len((e.get("sections") or {}).get("relations", []))
        return {
            "source_type":   "snapshot",
            "source":        str(self.path),
            "est_entities":  len(data),
            "est_relations": rels,
            "by_kind":       by_kind,
            "by_type":       by_type,
        }

    def iter_entities(self) -> Iterator[dict]:
        for e in self._load():
            if isinstance(e, dict) and e.get("id"):
                yield e

    def run(self, db_root: Path, db_name: str) -> ImportSummary:
        summary = super().run(db_root, db_name)
        # Rebuild the name index so name lookups / dedup work after the import.
        try:
            from aethviondb.name_index import NameIndex
            idx = NameIndex(index_path=db_root / "name_index.json")
            mapping = {e["name"]: e["id"] for e in self._load()
                       if e.get("name") and e.get("id")}
            idx.register_many(mapping)
        except Exception as exc:
            logger.warning(f"[snapshot import] name-index rebuild failed: {exc}")
            summary.warnings.append(f"name-index rebuild failed: {exc}")
        return summary
