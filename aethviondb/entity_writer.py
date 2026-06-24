"""
core/aethviondb/entity_writer.py
Layer-1 entity file creator and updater for AethvionDB.

Layer 1 files are the source of truth — they are append-only in spirit:
  • Never delete or overwrite raw facts; always increment version.
  • Never copy facts from another entity; reference by ID only.

All mutations go through EntityWriter, which:
  1. Consults the NameIndex before creating (prevents duplicates).
  2. Validates the schema on write.
  3. Writes atomically (temp-file + rename).
  4. Keeps a reverse ID→path map for fast lookups.

Storage layout
--------------
data/modes/worldsim/entities/
    ws_<hex>.json    — one file per entity
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from aethviondb._utils import get_logger, atomic_json_write
from aethviondb.config import AETHVIONDB

_DEFAULT_ENTITIES_DIR = AETHVIONDB / "default" / "entities"
from .entity_schema import make_empty, validate, _new_id, _now_iso, VALID_STATUSES, SCHEMA_VERSION
from .name_index import NameIndex, get_index
from . import snapshot as _snapshot

logger = get_logger(__name__)


class VersionConflictError(RuntimeError):
    """
    Raised when an optimistic-concurrency update is attempted with a stale
    ``expected_version``. The caller read the entity at one version, another
    writer advanced it, and applying the stale edit would silently clobber that
    change. The caller should re-read and rebase.
    """

    def __init__(self, entity_id: str, expected: int, actual: int) -> None:
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict on {entity_id!r}: expected v{expected}, "
            f"but current is v{actual}. Re-read and retry."
        )


# Per-entity write locks, keyed by the absolute entity-file path so they are
# shared across every EntityWriter instance pointing at the same database
# (the API builds a fresh writer per request). This makes the read-modify-write
# in update()/delete() atomic *within this process*.
#
# This is the seam for cross-process safety later: when the multiplayer/edge
# story arrives, a file lock (e.g. filelock.FileLock on "<path>.lock") composes
# in here without touching any call site.
_locks_guard: threading.Lock = threading.Lock()
_entity_locks: dict[str, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        lock = _entity_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _entity_locks[key] = lock
        return lock


class EntityWriter:
    """
    Create, read, and update WorldSim Layer-1 entity files.

    Parameters
    ----------
    entities_dir : Path, optional
        Directory where entity JSON files live.
        Defaults to data/modes/worldsim/entities/.
    index : NameIndex, optional
        The name→ID index to consult and update.
        Defaults to the module-level singleton.
    """

    def __init__(
        self,
        entities_dir: Optional[Path] = None,
        index: Optional[NameIndex] = None,
    ) -> None:
        self._dir = entities_dir or _DEFAULT_ENTITIES_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index = index or get_index()

    # Path helpers

    def _path_for(self, entity_id: str) -> Path:
        return self._dir / f"{entity_id}.json"

    # Atomic write

    def _write(self, entity: dict[str, Any]) -> None:
        # Stamp the on-disk format version here, the single chokepoint every
        # persisted entity passes through — so legacy records are upgraded the
        # next time they're written, and new ones always carry it.
        entity.setdefault("schema_version", SCHEMA_VERSION)
        atomic_json_write(self._path_for(entity["id"]), entity)
        # Patch the in-memory cache in place and bump the generation — O(1),
        # so a single write never triggers a full N-file snapshot rebuild.
        _snapshot.put(self._dir.parent, entity)

    # Public API

    def exists(self, entity_id: str) -> bool:
        return self._path_for(entity_id).exists()

    def get(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Load and return an entity by ID, or None if not found."""
        path = self._path_for(entity_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[EntityWriter] Failed to read {path}: {e}")
            return None

    def get_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Look up by name via the index, then load."""
        eid = self._index.get(name)
        if not eid:
            return None
        return self.get(eid)

    def create(
        self,
        name: str,
        entity_type: str = "other",
        source: str = "manual",
        sections_override: Optional[dict[str, Any]] = None,
        extra_aliases: Optional[list[str]] = None,
        kind: "str | list[str] | None" = None,
        status: str = "active",
    ) -> tuple[dict[str, Any], bool]:
        """
        Create a new entity file (or return the existing one if already indexed).

        Returns (entity_dict, was_created).
        was_created=False means the entity already existed in the index.
        """
        # Atomically claim the name — prevents duplicates under concurrent writes.
        # get_or_create holds the NameIndex lock across both the check and the
        # registration, so two threads racing on the same name will only create
        # one entity: the second caller gets was_new=False and returns early.
        candidate_id = _new_id()
        entity_id, was_new = self._index.get_or_create(name, candidate_id)

        resolved_status = status if status in VALID_STATUSES else "active"

        if not was_new:
            if self.exists(entity_id):
                existing = self.get(entity_id)
                if existing and existing.get("status") in ("deleted", "retired"):
                    # Soft-deleted entity with the same name — reactivate it and
                    # treat as freshly created so the caller re-populates all fields.
                    # This fixes the delete-then-re-add lifecycle: a file that is
                    # removed and later re-added (or renamed back) must fully
                    # resurface its module, class, and function entities.
                    existing["status"] = resolved_status
                    if sections_override:
                        for sk, sv in sections_override.items():
                            if sk in existing["sections"]:
                                if isinstance(existing["sections"][sk], dict) and isinstance(sv, dict):
                                    existing["sections"][sk].update(sv)
                                else:
                                    existing["sections"][sk] = sv
                            else:
                                existing["sections"][sk] = sv
                    self._write(existing)
                    logger.info(f"[EntityWriter] Reactivated '{name}' ({entity_id})")
                    return existing, True   # was_created=True so caller refreshes children
                logger.debug(f"[EntityWriter] '{name}' already exists as {entity_id}")
                return existing, False  # type: ignore[return-value]
            # Edge case: index entry points to a missing file — fall through and
            # recreate the file under the already-registered entity_id.
            logger.warning(
                f"[EntityWriter] Index entry for '{name}' → {entity_id} exists "
                "but file is missing; recreating."
            )

        entity = make_empty(name, entity_type, source, entity_id, kind=kind, status=resolved_status)

        if sections_override:
            for section_key, section_val in sections_override.items():
                if section_key in entity["sections"]:
                    if isinstance(entity["sections"][section_key], dict) and isinstance(section_val, dict):
                        entity["sections"][section_key].update(section_val)
                    else:
                        entity["sections"][section_key] = section_val
                else:
                    entity["sections"][section_key] = section_val

        # Register aliases
        aliases = entity["sections"]["core"].get("aliases", [])
        if extra_aliases:
            aliases.extend(extra_aliases)
        if aliases:
            self._index.register_aliases(entity_id, aliases)

        # Validate before writing
        errors = validate(entity)
        if errors:
            logger.warning(f"[EntityWriter] Schema warnings for '{name}': {errors}")

        self._write(entity)
        logger.info(f"[EntityWriter] Created entity: {name!r} ({entity_id})")
        return entity, True

    def update(
        self,
        entity_id: str,
        mutations: dict[str, Any],
        merge_sections: bool = True,
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Update an existing entity.

        *mutations* is a partial entity dict. Top-level non-section keys
        are overwritten directly. Section keys are deep-merged when
        merge_sections=True (default), or replaced when False.

        The version counter and updated timestamp are always incremented.

        Concurrency
        -----------
        The whole read-modify-write runs under a per-entity lock, so two writers
        in this process can never interleave and lose an update. Pass
        *expected_version* for optimistic concurrency: if the entity's current
        version differs, the edit is rejected with ``VersionConflictError``
        instead of silently clobbering the newer state.

        Returns the updated entity.
        """
        with _lock_for(self._path_for(entity_id)):
            entity = self.get(entity_id)
            if entity is None:
                raise FileNotFoundError(f"Entity {entity_id!r} not found")

            current_version = entity.get("version", 0)
            if expected_version is not None and current_version != expected_version:
                raise VersionConflictError(entity_id, expected_version, current_version)

            # Mutate top-level fields (except protected ones)
            protected = {"id", "created", "version", "sections"}
            old_name  = entity.get("name")
            for k, v in mutations.items():
                if k not in protected:
                    entity[k] = v

            # Propagate name change to NameIndex
            new_name = entity.get("name")
            if new_name and new_name != old_name:
                self._index.register(new_name, entity_id)
                if old_name:
                    self._index.unregister(old_name)

            # Merge or replace sections
            incoming_sections = mutations.get("sections", {})
            if incoming_sections:
                if merge_sections:
                    for sec, val in incoming_sections.items():
                        existing = entity["sections"].get(sec)
                        if isinstance(existing, dict) and isinstance(val, dict):
                            existing.update(val)
                        elif isinstance(existing, list) and isinstance(val, list):
                            # Append new items (dedup by json repr for simple cases)
                            seen = {json.dumps(x, sort_keys=True) for x in existing}
                            for item in val:
                                key = json.dumps(item, sort_keys=True)
                                if key not in seen:
                                    existing.append(item)
                                    seen.add(key)
                        else:
                            entity["sections"][sec] = val
                else:
                    entity["sections"].update(incoming_sections)

            # Bump metadata
            entity["version"] = current_version + 1
            entity["updated"] = _now_iso()

            errors = validate(entity)
            if errors:
                logger.warning(f"[EntityWriter] Schema warnings after update of {entity_id}: {errors}")

            self._write(entity)

            # Re-index any new aliases
            aliases = entity["sections"]["core"].get("aliases", [])
            if aliases:
                self._index.register_aliases(entity_id, aliases)

            logger.debug(f"[EntityWriter] Updated {entity_id} → v{entity['version']}")
            return entity

    def delete(self, entity_id: str, *, soft: bool = True) -> bool:
        """
        Mark an entity as deleted (soft) or remove its file (hard).
        Hard deletion is irreversible — use with caution.
        Returns True if the entity existed.
        """
        with _lock_for(self._path_for(entity_id)):
            if not self.exists(entity_id):
                return False
            if soft:
                entity = self.get(entity_id)
                entity["status"] = "deleted"    # type: ignore[index]
                entity["updated"] = _now_iso()  # type: ignore[index]
                self._write(entity)             # type: ignore[arg-type]
                logger.info(f"[EntityWriter] Soft-deleted {entity_id}")
            else:
                self._path_for(entity_id).unlink(missing_ok=True)
                _snapshot.remove(self._dir.parent, entity_id)
                logger.info(f"[EntityWriter] Hard-deleted {entity_id}")
            return True

    # Bulk operations

    def _raw_list_all(self) -> list[dict[str, Any]]:
        """Read every ws_*.json from disk and return all entities (all statuses).

        This is the slow O(N-files) path.  Call ``list_all()`` instead — it
        uses the snapshot cache when available and falls back to this method
        only when the snapshot is missing or stale.
        """
        results: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("ws_*.json")):
            try:
                results.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning(f"[EntityWriter] Could not read {path}: {exc}")
        return results

    def list_all(
        self,
        include_deleted: bool = False,
        use_snapshot: bool = True,
    ) -> list[dict[str, Any]]:
        """Return full entities, served from the in-memory cache.

        Parameters
        ----------
        include_deleted:
            When ``False`` (default) entities with ``status="deleted"`` are
            excluded.  The cache stores all statuses; filtering is applied at
            read time.
        use_snapshot:
            Set to ``False`` to bypass the cache entirely (always reads from
            individual entity files).  Use it for callers that need a guaranteed
            live view without touching the cache (e.g. mid-scan diagnostics).
        """
        if not use_snapshot:
            all_entities = self._raw_list_all()
            if include_deleted:
                return all_entities
            return [e for e in all_entities if e.get("status") != "deleted"]

        return _snapshot.get_all(
            self._dir.parent, self._dir, self._raw_list_all,
            include_deleted=include_deleted,
        )

    def list_lite(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        """Return the lightweight list-view projection of all entities.

        Each item carries only the columns the explorer renders (id, name,
        type, kind, status, summary, tags, counts, dates) — a fraction of the
        full payload.  Load full bodies on demand with ``get(entity_id)``.
        """
        return _snapshot.get_lite(
            self._dir.parent, self._dir, self._raw_list_all,
            include_deleted=include_deleted,
        )

    def count(self, include_deleted: bool = False) -> int:
        if include_deleted:
            # Glob count only — no file I/O needed.
            return sum(1 for _ in self._dir.glob("ws_*.json"))
        return len(self.list_all(include_deleted=False))

    def list_stubs(self) -> list[dict[str, Any]]:
        """Return all active stub entities (status='stub')."""
        return [e for e in self.list_all() if e.get("status") == "stub"]

    def get_stub_names_for(self, entity_id: str) -> list[str]:
        """Return stub names from sections.stubs that still need expansion.

        Includes:
        - Names not yet in the index (need to be created)
        - Names that ARE in the index but the entity is still status='stub'

        Excludes names whose entities are already fully expanded (status='active').
        """
        entity = self.get(entity_id)
        if not entity:
            return []
        stubs = entity["sections"].get("stubs", [])
        result: list[str] = []
        for name in stubs:
            existing_id = self._index.get(name)
            if not existing_id:
                result.append(name)          # doesn't exist yet — include
            else:
                existing_entity = self.get(existing_id)
                if existing_entity and existing_entity.get("status") == "stub":
                    result.append(name)      # exists but still a stub — include
                # else: already active — skip
        return result

    def search_by_type(self, entity_type: str) -> list[dict[str, Any]]:
        return [e for e in self.list_all() if e.get("type") == entity_type]

    def search_by_kind(self, kind: str) -> list[dict[str, Any]]:
        def _matches(e: dict[str, Any]) -> bool:
            ek = e.get("kind")
            if isinstance(ek, list):
                return kind in ek
            return ek == kind
        return [e for e in self.list_all() if _matches(e)]

    def search_by_tag(self, tag: str) -> list[dict[str, Any]]:
        tag_lower = tag.lower()
        return [
            e for e in self.list_all()
            if any(t.lower() == tag_lower for t in e["sections"]["core"].get("tags", []))
        ]
