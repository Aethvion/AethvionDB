"""
core/aethviondb/validator.py
Code-only integrity validator for AethvionDB entities.

No AI calls — purely deterministic rule checks. Run on every write or
on demand via the dashboard. Returns structured issues so callers can
decide whether to block, warn, or log.

Checks
------
1. Temporal consistency   — timeline events not anachronistic; dates parseable
2. Spatial plausibility   — entity can't be "located_in" two mutually exclusive
                            places simultaneously
3. Lifespan integrity     — birth before death; events within lifespan
4. Containment            — located_in relations don't form cycles
5. Reference integrity    — all target_id values point to real entities
6. Type consistency       — required properties present for known types
7. Self-reference         — entity must not relate to itself
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from aethviondb._utils import get_logger
from .entity_writer import EntityWriter

logger = get_logger(__name__)


class Severity(str, Enum):
    ERROR   = "error"    # Integrity violation — entity is corrupt
    WARNING = "warning"  # Suspicious but not necessarily wrong
    INFO    = "info"     # Informational note


@dataclass
class Issue:
    severity:  Severity
    check:     str         # Name of the check that raised this
    message:   str
    entity_id: str = ""
    field:     str = ""    # Which field/section triggered it


@dataclass
class ValidationResult:
    entity_id: str
    issues:    list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "ok":        self.ok,
            "issues": [
                {
                    "severity": i.severity.value,
                    "check":    i.check,
                    "message":  i.message,
                    "field":    i.field,
                }
                for i in self.issues
            ],
        }


# Name helpers

_WHITESPACE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    """Canonical form for duplicate comparison — matches NameIndex normalization."""
    return _WHITESPACE.sub(" ", name.strip()).lower()


# Date helpers

_BCE_RE    = re.compile(r"\bBC(E)?\b",             re.IGNORECASE)
_BCE_STRIP = re.compile(r"\s*(?:AD|CE|BC|BCE)\s*$", re.IGNORECASE)
# Full ISO date — supports negative years for geological time (e.g. -66000000-01-01)
_ISO_FULL  = re.compile(r"^(-?\d+)-(\d{2})-(\d{2})$")
# Partial ISO date without day component (e.g. 2025-10)
_ISO_PART  = re.compile(r"^(-?\d+)-(\d{2})$")
# Plain integer year — any digit count, sign allowed (e.g. -250000000, 950, 1810)
_YEAR_INT  = re.compile(r"^(-?\d+)$")
# Natural-language geological age: "4.54 billion years ago", "3.5 million years ago"
_NATURAL_AGO_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s+(billion|million|thousand)\s+years?\s+ago$",
    re.IGNORECASE,
)
_NATURAL_MULTIPLIERS = {"billion": 1_000_000_000, "million": 1_000_000, "thousand": 1_000}


def _parse_year(date_str: str) -> Optional[int]:
    """
    Extract a year integer from a date string.
    Returns None if unparseable.  Negative values = BCE / geological past.

    Handles:
      ~0950                    →         950  (Old English attested)
      ~1810                    →        1810  (modern era)
      2025-10                  →        2025  (partial ISO, year-month only)
      2025-10-15               →        2025  (full ISO)
      ~-250000000              → -250000000   (geological, ~250 Ma ago)
      ~-66000000               →  -66000000   (K-Pg boundary)
      250 BC / 250 BCE         →        -250  (explicit BCE suffix)
      ~4.54 billion years ago  → -4540000000  (natural language geological age)
      ~3.5 million years ago   →   -3500000   (natural language geological age)
    """
    s = date_str.strip()

    # Strip leading approximate marker
    if s.startswith("~"):
        s = s[1:].strip()

    # Natural-language geological age (checked before numeric patterns)
    m = _NATURAL_AGO_RE.match(s)
    if m:
        value      = float(m.group(1))
        multiplier = _NATURAL_MULTIPLIERS[m.group(2).lower()]
        return -int(value * multiplier)

    # Detect explicit BCE suffix *before* stripping it
    is_bce = bool(_BCE_RE.search(s))

    # Remove trailing era label so numeric patterns match cleanly
    s = _BCE_STRIP.sub("", s).strip()

    for pat in (_ISO_FULL, _ISO_PART, _YEAR_INT):
        m = pat.match(s)
        if m:
            year = int(m.group(1))
            # A leading minus already encodes "past" (geological convention).
            # An explicit BC/BCE suffix on a *positive* number also means past.
            if is_bce and year > 0:
                year = -year
            return year

    return None


# Individual checks

def _check_temporal(entity: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    timeline = entity["sections"].get("timeline", [])
    if not isinstance(timeline, list):
        return issues

    years = []
    for i, ev in enumerate(timeline):
        if not isinstance(ev, dict):
            continue
        date_str = ev.get("date", "")
        year = _parse_year(date_str)
        if year is None and date_str:
            issues.append(Issue(
                Severity.WARNING, "temporal",
                f"timeline[{i}]: unparseable date {date_str!r}",
                entity["id"], f"timeline[{i}].date",
            ))
        elif year is not None:
            years.append((i, year))

    # Check ordering (events should be roughly chronological)
    for idx in range(1, len(years)):
        prev_i, prev_y = years[idx - 1]
        cur_i,  cur_y  = years[idx]
        if cur_y < prev_y - 5:  # allow 5-year slack for approximations
            issues.append(Issue(
                Severity.WARNING, "temporal",
                f"timeline[{cur_i}] ({cur_y}) appears before timeline[{prev_i}] ({prev_y}) — out of order?",
                entity["id"], f"timeline[{cur_i}].date",
            ))

    return issues


def _check_lifespan(entity: dict[str, Any]) -> list[Issue]:
    """For person/creature entities: birth must precede death."""
    issues: list[Issue] = []
    if entity.get("type") not in ("person", "creature", "species"):
        return issues

    props = entity["sections"].get("properties", {})
    birth_str = props.get("birth_date") or props.get("born") or props.get("birth_year")
    death_str = props.get("death_date") or props.get("died") or props.get("death_year")

    if not birth_str or not death_str:
        return issues

    birth = _parse_year(str(birth_str))
    death = _parse_year(str(death_str))

    if birth is None or death is None:
        return issues

    if death < birth:
        issues.append(Issue(
            Severity.ERROR, "lifespan",
            f"Death ({death_str}) precedes birth ({birth_str})",
            entity["id"], "properties",
        ))
    elif death - birth > 200:
        issues.append(Issue(
            Severity.WARNING, "lifespan",
            f"Lifespan of {death - birth} years seems implausible",
            entity["id"], "properties",
        ))

    # Check timeline events fall within lifespan (loose check)
    timeline = entity["sections"].get("timeline", [])
    for i, ev in enumerate(timeline):
        if not isinstance(ev, dict):
            continue
        ev_year = _parse_year(ev.get("date", ""))
        if ev_year is None:
            continue
        if ev_year < birth - 5:
            issues.append(Issue(
                Severity.WARNING, "lifespan",
                f"timeline[{i}] ({ev_year}) is before entity birth year ({birth})",
                entity["id"], f"timeline[{i}].date",
            ))
        if ev_year > death + 10:
            issues.append(Issue(
                Severity.WARNING, "lifespan",
                f"timeline[{i}] ({ev_year}) is long after entity death ({death})",
                entity["id"], f"timeline[{i}].date",
            ))

    return issues


def _check_self_reference(entity: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    eid = entity["id"]
    for i, rel in enumerate(entity["sections"].get("relations", [])):
        if isinstance(rel, dict) and rel.get("target_id") == eid:
            issues.append(Issue(
                Severity.ERROR, "self_reference",
                f"relations[{i}] points to itself ({eid})",
                eid, f"relations[{i}]",
            ))
    return issues


def _check_reference_integrity(
    entity: dict[str, Any],
    writer: EntityWriter,
) -> list[Issue]:
    """All target_id values in relations must exist in the entity store."""
    issues: list[Issue] = []
    for i, rel in enumerate(entity["sections"].get("relations", [])):
        if not isinstance(rel, dict):
            continue
        target_id = rel.get("target_id", "")
        if target_id and not writer.exists(target_id):
            issues.append(Issue(
                Severity.ERROR, "reference_integrity",
                f"relations[{i}].target_id {target_id!r} does not exist",
                entity["id"], f"relations[{i}].target_id",
            ))
    # Also check timeline ref_ids
    for i, ev in enumerate(entity["sections"].get("timeline", [])):
        if not isinstance(ev, dict):
            continue
        for j, ref_id in enumerate(ev.get("ref_ids", [])):
            if ref_id and not writer.exists(ref_id):
                issues.append(Issue(
                    Severity.WARNING, "reference_integrity",
                    f"timeline[{i}].ref_ids[{j}] {ref_id!r} does not exist",
                    entity["id"], f"timeline[{i}].ref_ids[{j}]",
                ))
    return issues


def _check_containment_cycle(
    entity: dict[str, Any],
    writer: EntityWriter,
    max_depth: int = 10,
) -> list[Issue]:
    """
    Detect cycles in located_in / part_of / member_of / child_of chains.

    Uses a backtracking DFS that follows *all* containment relations for
    each entity (not just the first one).  Per-branch path tracking avoids
    false positives from diamond-shaped hierarchies where two paths legitimately
    converge on the same ancestor.
    """
    issues: list[Issue] = []
    start_id = entity["id"]
    containment_kinds = {"located_in", "part_of", "member_of", "child_of"}
    reported: set[str] = set()   # avoid emitting duplicate cycle reports

    def _dfs(current_id: str, path: set, depth: int) -> None:
        if depth > max_depth:
            return
        current = writer.get(current_id)
        if not current:
            return
        for rel in current["sections"].get("relations", []):
            if not isinstance(rel, dict) or rel.get("kind") not in containment_kinds:
                continue
            parent_id = rel.get("target_id")
            if not parent_id:
                continue
            if parent_id in path:
                if parent_id not in reported:
                    issues.append(Issue(
                        Severity.ERROR, "containment_cycle",
                        f"Containment cycle detected: {start_id} → … → {parent_id} (revisit)",
                        start_id, "relations",
                    ))
                    reported.add(parent_id)
                continue   # don't recurse further through this cycle node
            path.add(parent_id)
            _dfs(parent_id, path, depth + 1)
            path.discard(parent_id)   # backtrack

    _dfs(start_id, {start_id}, 0)
    return issues


def _check_stub_status_mismatch(entity: dict[str, Any]) -> list[Issue]:
    """
    Entity is marked 'stub' but already has a non-empty summary —
    status was never promoted to 'active' after content was written.
    """
    issues: list[Issue] = []
    if entity.get("status") != "stub":
        return issues
    summary = entity["sections"]["core"].get("summary", "")
    if summary:
        issues.append(Issue(
            Severity.WARNING, "status_mismatch",
            "Entity is marked 'stub' but has a non-empty summary — "
            "status should be promoted to 'active'",
            entity["id"], "status",
        ))
    return issues


def _check_type_consistency(entity: dict[str, Any]) -> list[Issue]:
    """Type-specific required property checks."""
    issues: list[Issue] = []
    etype   = entity.get("type", "other")
    props   = entity["sections"].get("properties", {})
    summary = entity["sections"]["core"].get("summary", "")

    # Only warn about missing summary for active entities; stubs are expected to be incomplete.
    if not summary and entity.get("status") == "active":
        issues.append(Issue(
            Severity.WARNING, "type_consistency",
            "core.summary is empty — entity has no description",
            entity["id"], "sections.core.summary",
        ))

    if etype == "person" and entity.get("status") == "active":
        if not props.get("birth_date") and not props.get("birth_year") and not props.get("born"):
            issues.append(Issue(
                Severity.INFO, "type_consistency",
                "Person entity has no birth date in properties",
                entity["id"], "sections.properties",
            ))

    elif etype == "place":
        location_kinds = {"located_in", "part_of", "contains"}
        has_location_rel = any(
            rel.get("kind") in location_kinds
            for rel in entity["sections"].get("relations", [])
            if isinstance(rel, dict)
        )
        if not has_location_rel and not props.get("coordinates") and not props.get("country"):
            issues.append(Issue(
                Severity.INFO, "type_consistency",
                "Place entity has no location relation or coordinates",
                entity["id"], "sections.relations",
            ))

    elif etype == "event":
        if not entity["sections"].get("timeline"):
            issues.append(Issue(
                Severity.WARNING, "type_consistency",
                "Event entity has an empty timeline",
                entity["id"], "sections.timeline",
            ))

    return issues


# Cross-entity duplicate detection

def _entity_score(entity: dict[str, Any]) -> int:
    """
    Content-richness score used to pick the preferred entity in a duplicate
    cluster.  Higher = more developed; that entity becomes the recommended
    primary (the one we keep).
    """
    score = {"active": 20, "stub": 5, "deleted": 0}.get(entity.get("status", "stub"), 5)
    sections = entity.get("sections", {})
    core     = sections.get("core", {})
    summary  = core.get("summary", "") or ""
    score += min(len(summary) // 50, 10)               # up to +10 for long summary
    score += min(len(core.get("aliases", [])), 5)
    score += min(len(sections.get("timeline",  [])) * 2, 20)
    score += min(len(sections.get("relations", [])) * 2, 20)
    score += min(len(sections.get("properties", {}) or {}), 10)
    score += min(entity.get("version", 1), 10)         # more edit rounds = more developed
    return score


def _collect_duplicate_groups(
    entities: list[dict[str, Any]],
) -> tuple[dict[str, list[Issue]], list[dict[str, Any]]]:
    """
    Scan all entities for name / alias collisions.

    Two entities are considered duplicates when their primary name *or* any
    alias normalises to the same string.

    Returns
    -------
    issues_by_id
        entity_id → [Issue] for every entity that participates in at least one
        duplicate group.  Each Issue carries Severity.ERROR so the entity shows
        up in ``failed_ids`` in the summary.
    groups
        List of dicts suitable for the API response.  Each entry:
        ``{norm_name, ids, names, entities, recommended_primary,
           recommended_remove, action}``
        where ``action`` is ``"auto"`` when there is a clear winner (e.g. one
        active + N stubs) or ``"choose"`` when human judgment is needed.
        Each entry in ``entities`` is sorted by score descending.
    """
    # Aliases shorter than this many characters are element symbols / scientific
    # abbreviations (e.g. "N", "Sm", "Fe") that cause false-positive duplicate
    # matches.  Primary names are always checked regardless of length.
    _MIN_ALIAS_LEN = 4

    # Build normalised-name → [entity_id, ...] map (primary name + all aliases)
    name_to_ids: dict[str, list[str]] = defaultdict(list)
    id_to_entity: dict[str, dict[str, Any]] = {}

    for entity in entities:
        eid          = entity["id"]
        id_to_entity[eid] = entity
        primary_name = entity.get("name", "")
        aliases      = entity["sections"]["core"].get("aliases", [])

        for raw in [primary_name, *aliases]:
            if not raw:
                continue
            key       = _normalize_name(raw)
            is_alias  = (raw != primary_name)
            # Short aliases (< _MIN_ALIAS_LEN chars after normalisation) are
            # abbreviations / symbols that match unrelated entities.
            if is_alias and len(key) < _MIN_ALIAS_LEN:
                continue
            name_to_ids[key].append(eid)

    issues_by_id: dict[str, list[Issue]] = {}
    groups: list[dict[str, Any]] = []
    # Deduplicate: same set of entity IDs may match via multiple shared names.
    seen_clusters: set[frozenset] = set()

    for norm_name, ids in name_to_ids.items():
        unique_ids = list(dict.fromkeys(ids))   # preserve first-seen order
        if len(unique_ids) < 2:
            continue
        cluster_key = frozenset(unique_ids)
        if cluster_key in seen_clusters:
            continue
        seen_clusters.add(cluster_key)

        # Sort by richness score so the recommended primary is first
        scored_ids = sorted(unique_ids, key=lambda i: _entity_score(id_to_entity[i]), reverse=True)
        primary_id  = scored_ids[0]
        remove_ids  = scored_ids[1:]

        # Build per-entity summary cards (rich enough to drive UI decisions)
        entity_cards: list[dict[str, Any]] = []
        for eid in scored_ids:
            e        = id_to_entity[eid]
            sections = e.get("sections", {})
            core     = sections.get("core", {})
            summary  = (core.get("summary") or "")[:120]
            entity_cards.append({
                "id":             eid,
                "name":           e.get("name", eid),
                "status":         e.get("status", "stub"),
                "summary":        summary,
                "has_summary":    bool(core.get("summary")),
                "alias_count":    len(core.get("aliases", [])),
                "relation_count": len(sections.get("relations", [])),
                "timeline_count": len(sections.get("timeline", [])),
                "version":        e.get("version", 1),
                "score":          _entity_score(e),
            })

        # action:
        #   "auto"      — clear winner: at least one active entity, rest are stubs
        #   "stub_auto" — all entities are stubs; safe to auto-remove lower-scored ones
        #   "choose"    — all active or mixed non-stub; needs human judgment
        statuses   = [c["status"] for c in entity_cards]
        has_active = "active" in statuses
        all_active = all(s == "active" for s in statuses)
        all_stubs  = all(s == "stub"   for s in statuses)
        action = ("auto"      if (has_active and not all_active)
             else "stub_auto" if all_stubs
             else "choose")

        groups.append({
            "norm_name":          norm_name,
            # Legacy flat lists kept for backward compatibility
            "ids":                unique_ids,
            "names":              [id_to_entity[i].get("name", i) for i in unique_ids],
            # Rich resolution data
            "entities":           entity_cards,
            "recommended_primary": primary_id,
            "recommended_remove":  remove_ids,
            "action":             action,
        })

        for eid in unique_ids:
            others_str = ", ".join(
                f"{id_to_entity.get(i, {}).get('name', i)} ({i})"
                for i in unique_ids
                if i != eid
            )
            issues_by_id.setdefault(eid, []).append(Issue(
                Severity.ERROR,
                "duplicate_entity",
                f"Name/alias {norm_name!r} is shared with: {others_str}",
                eid,
                "name",
            ))

    return issues_by_id, groups


# Main Validator class

class Validator:
    """
    Run all integrity checks on one or more entities.

    Usage
    -----
    v = Validator(writer)
    result = v.validate("ws_abc123")
    if not result.ok:
        for issue in result.errors:
            print(issue.message)

    # Validate entire store
    results = v.validate_all()
    """

    def __init__(self, writer: Optional[EntityWriter] = None) -> None:
        self._writer = writer or EntityWriter()

    def validate(self, entity_id: str) -> ValidationResult:
        """Run all checks on a single entity."""
        entity = self._writer.get(entity_id)
        if not entity:
            return ValidationResult(
                entity_id=entity_id,
                issues=[Issue(Severity.ERROR, "not_found", f"Entity {entity_id!r} not found", entity_id)],
            )

        result = ValidationResult(entity_id=entity_id)

        result.issues.extend(_check_stub_status_mismatch(entity))
        result.issues.extend(_check_temporal(entity))
        result.issues.extend(_check_lifespan(entity))
        result.issues.extend(_check_self_reference(entity))
        result.issues.extend(_check_reference_integrity(entity, self._writer))
        result.issues.extend(_check_containment_cycle(entity, self._writer))
        result.issues.extend(_check_type_consistency(entity))

        if result.issues:
            logger.debug(
                f"[Validator] {entity_id}: "
                f"{len(result.errors)} errors, {len(result.warnings)} warnings"
            )

        return result

    def validate_all(self, include_deleted: bool = False) -> list[ValidationResult]:
        """Run all checks on every entity in the store (including duplicate detection)."""
        entities = self._writer.list_all(include_deleted=include_deleted)
        dup_issues, _ = _collect_duplicate_groups(entities)
        results: list[ValidationResult] = []
        for entity in entities:
            r = self.validate(entity["id"])
            # Duplicate issues are cross-entity so they can't be raised by the
            # single-entity validate() path — inject them here at the front so
            # they appear first in the issue list.
            if entity["id"] in dup_issues:
                r.issues = dup_issues[entity["id"]] + r.issues
            results.append(r)
        return results

    # Human-readable label for each check name (used in warning_summary)
    _CHECK_LABELS: dict[str, str] = {
        "type_consistency":    "Missing or incomplete descriptions",
        "temporal":            "Timeline ordering issues",
        "lifespan":            "Lifespan integrity issues",
        "self_reference":      "Self-referencing relations",
        "reference_integrity": "Broken entity references",
        "containment_cycle":   "Containment cycles",
        "status_mismatch":     "Status-summary mismatches",
        "not_found":           "Missing entity files",
        "duplicate_entity":    "Duplicate names / aliases",
    }

    def summary(self) -> dict[str, Any]:
        """Return aggregate statistics for all entities."""
        entities = self._writer.list_all()

        # Cross-entity duplicate detection (done once for all entities)
        dup_issues, dup_groups = _collect_duplicate_groups(entities)

        # Orphan stub detection — stubs with no outgoing relations and not
        # referenced by any other entity (via relations or timeline ref_ids).
        _referenced_ids: set[str] = set()
        for _e in entities:
            for _rel in _e.get("sections", {}).get("relations", []):
                if isinstance(_rel, dict) and _rel.get("target_id"):
                    _referenced_ids.add(_rel["target_id"])
            for _ev in _e.get("sections", {}).get("timeline", []):
                if isinstance(_ev, dict):
                    for _ref in _ev.get("ref_ids", []):
                        _referenced_ids.add(_ref)

        orphan_stubs: list[dict[str, Any]] = [
            {"id": _e["id"], "name": _e.get("name", _e["id"])}
            for _e in entities
            if _e.get("status") == "stub"
            and not _e.get("sections", {}).get("relations", [])
            and _e["id"] not in _referenced_ids
        ]

        results: list[ValidationResult] = []
        stub_mismatches:       list[dict[str, str]]         = []
        entities_with_errors:  list[dict[str, Any]]         = []
        warning_counts:        dict[str, int]               = {}
        warning_entities:      dict[str, list[dict]]        = {}  # check → [{id, name, message}]

        for entity in entities:
            r = self.validate(entity["id"])
            if entity["id"] in dup_issues:
                r.issues = dup_issues[entity["id"]] + r.issues
            results.append(r)

            # Status-mismatch list (fixable via /validate/fix-status-mismatches)
            if entity.get("status") == "stub" and entity["sections"]["core"].get("summary"):
                stub_mismatches.append({
                    "id":   entity["id"],
                    "name": entity.get("name", entity["id"]),
                })

            # Per-entity error detail — strip pure duplicate_entity issues since those
            # already appear in the dedicated Duplicate Groups section.  Include every
            # entity that has remaining real errors, even if it's also in a dup group.
            real_errors = [i for i in r.errors if i.check != "duplicate_entity"]
            if real_errors:
                entities_with_errors.append({
                    "id":     entity["id"],
                    "name":   entity.get("name", entity["id"]),
                    "issues": [
                        {"check": i.check, "message": i.message, "field": i.field}
                        for i in real_errors
                    ],
                })

            # Warning counts + per-entity breakdown (so the UI can show which
            # entities are affected for each warning type, not just totals).
            for i in r.warnings:
                warning_counts[i.check] = warning_counts.get(i.check, 0) + 1
                warning_entities.setdefault(i.check, []).append({
                    "id":      entity["id"],
                    "name":    entity.get("name", entity["id"]),
                    "message": i.message,
                })

        total_errors   = sum(len(r.errors)   for r in results)
        total_warnings = sum(len(r.warnings) for r in results)
        failed = [r.entity_id for r in results if not r.ok]

        warning_summary = [
            {
                "check":    check,
                "count":    count,
                "label":    self._CHECK_LABELS.get(check, check),
                "entities": warning_entities.get(check, []),
            }
            for check, count in sorted(warning_counts.items(), key=lambda x: -x[1])
        ]

        # Soft-deleted entities still live on disk — surface them so the UI
        # can offer a Purge action to permanently remove the files.
        deleted_entities: list[dict[str, Any]] = [
            {"id": e["id"], "name": e.get("name", e["id"])}
            for e in self._writer.list_all(include_deleted=True)
            if e.get("status") == "deleted"
        ]

        return {
            "total_entities":       len(results),
            "clean":                sum(1 for r in results if r.ok),
            "with_errors":          len(failed),
            "total_errors":         total_errors,
            "total_warnings":       total_warnings,
            "failed_ids":           failed,
            "stub_mismatches":      stub_mismatches,      # fixable via /validate/fix-status-mismatches
            "duplicate_groups":     dup_groups,            # each: {norm_name, ids, entities, …}
            "entities_with_errors": entities_with_errors,  # non-dup entities with actual errors
            "warning_summary":      warning_summary,       # [{check, count, label}] sorted by count
            "orphan_stubs":         orphan_stubs,          # stubs with no connections or refs
            "deleted_entities":     deleted_entities,      # soft-deleted files pending purge
        }
