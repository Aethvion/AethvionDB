"""
core/aethviondb/kind_registry.py
Kind Registry for AethvionDB.

Tracks all known "kinds" — fine-grained categorizations layered on top of
the base entity type. Each kind carries metadata: description, icon/color
hints, default properties, and common relation suggestions.

Storage: per-database sidecar  AethvionDB.KINDREGISTRY
Format : JSON { version, kinds: { kind_name: KindDef } }

Thread-safe via a per-instance lock (same pattern as NameIndex).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aethviondb._utils import get_logger

logger = get_logger(__name__)

SIDECAR = "AethvionDB.KINDREGISTRY"


# ---------------------------------------------------------------------------
# Built-in software kinds — pre-populated on first init_software_kinds() call.
# ---------------------------------------------------------------------------

DEFAULT_SOFTWARE_KINDS: dict[str, dict[str, Any]] = {
    "software.module": {
        "description": "A code module, file, package, or logical grouping of related code.",
        "icon": "📦",
        "color": "#6366f1",
        "default_properties": {"language": "", "file_path": "", "framework": ""},
        "common_relations": ["imports", "imported_by", "depends_on", "exposes", "contains", "tested_by"],
    },
    "software.service": {
        "description": "A deployable service, microservice, or external integration.",
        "icon": "⚙️",
        "color": "#0ea5e9",
        "default_properties": {"language": "", "port": "", "protocol": "", "host": ""},
        "common_relations": ["exposes", "depends_on", "calls", "reads_from", "writes_to", "owns"],
    },
    "software.component": {
        "description": "A UI, architectural, or functional unit within a service or module.",
        "icon": "🧩",
        "color": "#8b5cf6",
        "default_properties": {"framework": "", "layer": ""},
        "common_relations": ["part_of", "uses", "calls", "exposes", "depends_on"],
    },
    "software.class": {
        "description": "A code class, interface, or abstract type definition.",
        "icon": "🏗️",
        "color": "#f59e0b",
        "default_properties": {"language": "", "file_path": "", "is_abstract": ""},
        "common_relations": ["extends", "implements", "part_of", "uses", "calls"],
    },
    "software.function": {
        "description": "A function, method, procedure, or callable unit.",
        "icon": "⚡",
        "color": "#10b981",
        "default_properties": {"language": "", "file_path": "", "signature": ""},
        "common_relations": ["calls", "called_by", "part_of", "uses", "reads_from", "writes_to"],
    },
    "software.endpoint": {
        "description": "An API endpoint, HTTP route, or RPC method.",
        "icon": "🔌",
        "color": "#ef4444",
        "default_properties": {"method": "", "path": "", "auth": "", "response_format": ""},
        "common_relations": ["exposed_by", "calls", "reads_from", "writes_to", "documented_by"],
    },
    "software.model": {
        "description": "A data model, schema, database table, or type definition.",
        "icon": "🗃️",
        "color": "#f97316",
        "default_properties": {"storage": "", "format": "", "primary_key": ""},
        "common_relations": ["owned_by", "used_by", "reads_from", "writes_to", "related_to"],
    },
    "software.workflow": {
        "description": "A process, pipeline, job, or sequence of automated steps.",
        "icon": "🔄",
        "color": "#14b8a6",
        "default_properties": {"trigger": "", "schedule": "", "runtime": ""},
        "common_relations": ["triggers", "triggered_by", "reads_from", "writes_to", "uses"],
    },
    "software.config": {
        "description": "Configuration, environment settings, secrets, or feature flags.",
        "icon": "⚙️",
        "color": "#94a3b8",
        "default_properties": {"format": "", "scope": "", "env": ""},
        "common_relations": ["configures", "used_by", "part_of"],
    },
    "software.dependency": {
        "description": "An external library, package, framework, or third-party tool.",
        "icon": "📎",
        "color": "#a78bfa",
        "default_properties": {"version": "", "registry": "", "license": ""},
        "common_relations": ["dependency_of", "used_by", "implements"],
    },
    "software.decision": {
        "description": "An architectural decision record (ADR), tech choice, or design rationale.",
        "icon": "📋",
        "color": "#e879f9",
        "default_properties": {"status": "", "date": "", "alternatives_considered": ""},
        "common_relations": ["influences", "related_to", "documents", "preceded_by", "followed_by"],
    },
    "software.goal": {
        "description": "A roadmap item, planned feature, milestone, or strategic objective.",
        "icon": "🎯",
        "color": "#22d3ee",
        "default_properties": {"priority": "", "target_date": "", "success_criteria": ""},
        "common_relations": ["related_to", "part_of", "followed_by", "preceded_by"],
    },
    "software.constraint": {
        "description": "A technical constraint, non-functional requirement, or hard limitation.",
        "icon": "🔒",
        "color": "#fb7185",
        "default_properties": {"category": "", "severity": ""},
        "common_relations": ["related_to", "influences", "configured_by"],
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KindRegistry:
    """
    Per-database kind registry backed by AethvionDB.KINDREGISTRY sidecar.

    Parameters
    ----------
    db_root : Path
        Root directory of the database (same level as entities/, chunks/, etc.).
    """

    def __init__(self, db_root: Path) -> None:
        self._path = db_root / SIDECAR
        self._lock = threading.Lock()
        self._data = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"[KindRegistry] Could not read {self._path}: {exc}")
        return {"version": 1, "kinds": {}}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".KINDREGISTRY.tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.error(f"[KindRegistry] Could not save {self._path}: {exc}")
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[dict[str, Any]]:
        """Return the kind definition for *name*, or None if not registered."""
        with self._lock:
            return self._data["kinds"].get(name)

    def register(
        self,
        name: str,
        *,
        description: str = "",
        icon: str = "",
        color: str = "",
        default_properties: Optional[dict[str, str]] = None,
        common_relations: Optional[list[str]] = None,
        required_properties: Optional[list[str]] = None,
        auto_registered: bool = False,
    ) -> dict[str, Any]:
        """
        Register or fully replace a kind definition.
        Returns the stored definition.

        ``default_properties`` / ``common_relations`` are suggestions (for UI and
        autocomplete). ``required_properties`` is the opt-in *enforcement* surface:
        the validator warns when an active entity of this kind lacks one of them.
        """
        kind_def: dict[str, Any] = {
            "name": name,
            "description": description,
            "icon": icon,
            "color": color,
            "default_properties": default_properties or {},
            "common_relations": common_relations or [],
            "required_properties": required_properties or [],
            "auto_registered": auto_registered,
            "created_at": _now_iso(),
        }
        with self._lock:
            existing = self._data["kinds"].get(name)
            if existing:
                kind_def["created_at"] = existing.get("created_at", kind_def["created_at"])
            self._data["kinds"][name] = kind_def
            self._save()
        logger.debug(f"[KindRegistry] Registered kind: {name!r} (auto={auto_registered})")
        return kind_def

    def auto_register(self, name: str) -> tuple[dict[str, Any], bool]:
        """
        Ensure *name* is in the registry. If already known, return it unchanged.
        If unknown, register it with minimal metadata and auto_registered=True.

        Returns (kind_def, was_new).
        """
        with self._lock:
            existing = self._data["kinds"].get(name)
            if existing:
                return existing, False

        # Not found — register outside the lock (register() acquires it again)
        kind_def = self.register(name, auto_registered=True)
        logger.info(f"[KindRegistry] Auto-registered new kind: {name!r}")
        return kind_def, True

    def update(self, name: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        """
        Partially update an existing kind definition.
        Returns the updated definition, or None if the kind doesn't exist.
        Allowed keys: description, icon, color, default_properties, common_relations.
        """
        allowed = {"description", "icon", "color", "default_properties",
                   "common_relations", "required_properties"}
        with self._lock:
            existing = self._data["kinds"].get(name)
            if existing is None:
                return None
            for k, v in kwargs.items():
                if k in allowed:
                    existing[k] = v
            existing["auto_registered"] = False  # manual update clears the auto flag
            self._save()
        return existing

    def delete(self, name: str) -> bool:
        """Remove a kind from the registry. Returns True if it existed."""
        with self._lock:
            if name not in self._data["kinds"]:
                return False
            del self._data["kinds"][name]
            self._save()
        logger.info(f"[KindRegistry] Deleted kind: {name!r}")
        return True

    def list_all(self, prefix: str = "") -> list[dict[str, Any]]:
        """
        Return all registered kinds, optionally filtered by name prefix.
        Results are sorted by name.
        """
        with self._lock:
            kinds = list(self._data["kinds"].values())
        if prefix:
            kinds = [k for k in kinds if k["name"].startswith(prefix)]
        return sorted(kinds, key=lambda k: k["name"])

    def init_software_kinds(self) -> int:
        """
        Populate all built-in software kinds. Skips kinds already registered.
        Returns the count of newly added kinds.
        """
        added = 0
        for name, defaults in DEFAULT_SOFTWARE_KINDS.items():
            _, was_new = self.auto_register(name)
            if was_new:
                # Replace the minimal auto-registration with full defaults
                self.register(name, auto_registered=False, **defaults)
                added += 1
        logger.info(f"[KindRegistry] init_software_kinds: added {added} new kinds")
        return added

    def stats(self) -> dict[str, Any]:
        with self._lock:
            kinds = self._data["kinds"]
        total = len(kinds)
        auto  = sum(1 for k in kinds.values() if k.get("auto_registered"))
        prefixes: dict[str, int] = {}
        for name in kinds:
            prefix = name.split(".")[0] if "." in name else "_root"
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        return {"total": total, "auto_registered": auto, "by_prefix": prefixes}
