"""
core/aethviondb/distiller.py
General content distiller for AethvionDB.

Paste any text — a book excerpt, an article, raw notes, a document — and
the AI reads it, identifies the primary entity being described, and writes
a structured Layer-1 entity file.

The distiller determines the entity name itself from the content.
No manual title input is required.

Usage
-----
    from aethviondb.distiller import ContentDistiller
    from aethviondb.entity_writer import EntityWriter
    from aethviondb.name_index import NameIndex

    writer = EntityWriter(entities_dir=Path("/my/db/entities"))
    index  = NameIndex(index_path=Path("/my/db/name_index.json"))
    d = ContentDistiller(writer=writer, index=index)

    result = await d.distill(content="...", model="gemini-1.5-flash")
    # result["entity_name"] contains what the AI decided the name is
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Optional

from aethviondb._utils import get_logger
from .ai_runtime import get_llm_caller
from .entity_schema import VALID_TYPES
from .entity_writer import EntityWriter
from .name_index import NameIndex
from .file_manifest import FileManifest, detect_language

logger = get_logger(__name__)

# System prompt

_SYSTEM_PROMPT = """You are a knowledge extraction engine for a structured database.

Read the provided text and extract all key information into a strict JSON object.

RULES:
- Output ONLY valid JSON. No markdown, no code fences, no explanation.
- "name" is the canonical name of the primary subject of the text (required).
- "type" must be exactly one of:
  General: person, place, event, concept, organization, artifact, creature,
           substance, process, phenomenon, work, species, universe, other
  Software: module, service, component, class, function, endpoint,
            model, workflow, config, dependency, decision, goal, constraint
- "timeline" dates must be YYYY or YYYY-MM-DD (approximate: "~1900").
- "stubs" lists only meaningful entities (people, places, orgs, concepts, modules,
  services, classes) that appear in the text and deserve their own entry.
- "relations" kinds must be one of:
  General: parent_of, child_of, member_of, contains, created_by, created,
           located_in, location_of, part_of, has_part, preceded_by, followed_by,
           related_to, instance_of, has_instance, influenced_by, influenced,
           participated_in, has_participant
  Software: calls, called_by, imports, imported_by, depends_on, dependency_of,
            implements, implemented_by, extends, extended_by, uses, used_by,
            exposes, exposed_by, configures, configured_by, tests, tested_by,
            documents, documented_by, replaced_by, replaces, deprecated_by,
            deprecates, owns, owned_by, reads_from, read_by, writes_to,
            written_by, triggers, triggered_by
- Do not invent facts not stated in the text. Use "" or [] for unknown fields.

Required output structure (no extra keys):
{
  "name": "Canonical Name",
  "type": "<entity_type>",
  "aliases": ["alternate name"],
  "categories": ["Category"],
  "tags": ["keyword"],
  "summary": "1-3 sentence essence of what this entity is",
  "timeline": [
    { "date": "YYYY", "event": "What happened", "ref_names": ["Name"] }
  ],
  "relations": [
    { "kind": "related_to", "target_name": "Name", "note": "" }
  ],
  "properties": {
    "key": "value"
  },
  "stubs": ["Sub-topic name"]
}"""


def _build_prompt(content: str, max_chars: int = 12000) -> str:
    truncated = content[:max_chars]
    if len(content) > max_chars:
        truncated += "\n\n[...truncated...]"
    return f"Text to distill:\n\n{truncated}"


def _extract_json(raw: str) -> dict[str, Any]:
    clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    start = clean.find("{")
    end   = clean.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in response")
    return json.loads(clean[start:end + 1])


def _map_to_sections(
    extracted: dict[str, Any],
    index: NameIndex,
    writer: EntityWriter,
) -> tuple[dict[str, Any], list[str]]:
    """Map extracted dict → entity sections. Returns (sections, unresolved_stubs)."""

    core = {
        "summary":    str(extracted.get("summary", "")),
        "aliases":    [str(a) for a in extracted.get("aliases", []) if a],
        "categories": [str(c) for c in extracted.get("categories", []) if c],
        "tags":       [str(t) for t in extracted.get("tags", []) if t],
    }

    timeline = []
    for ev in extracted.get("timeline", []):
        if not isinstance(ev, dict):
            continue
        entry: dict[str, Any] = {
            "date":  str(ev.get("date", "")),
            "event": str(ev.get("event", "")),
        }
        ref_ids = [index.get(str(n)) for n in ev.get("ref_names", []) if index.get(str(n))]
        if ref_ids:
            entry["ref_ids"] = ref_ids
        if entry["date"] and entry["event"]:
            timeline.append(entry)

    relations = []
    for rel in extracted.get("relations", []):
        if not isinstance(rel, dict):
            continue
        target_name = str(rel.get("target_name", ""))
        if not target_name:
            continue
        target_id = index.get(target_name)
        if not target_id:
            stub, _ = writer.create(target_name, entity_type="other", source="stub")
            writer.update(stub["id"], {"status": "stub"})
            target_id = stub["id"]
        entry: dict[str, Any] = {
            "kind":      str(rel.get("kind", "related_to")),
            "target_id": target_id,
        }
        if rel.get("note"):
            entry["note"] = str(rel["note"])
        relations.append(entry)

    properties = {str(k): str(v) for k, v in extracted.get("properties", {}).items() if v is not None}
    stubs = [str(s) for s in extracted.get("stubs", []) if s and not index.get(str(s))]

    return {
        "core":       core,
        "timeline":   timeline,
        "relations":  relations,
        "properties": properties,
        "stubs":      stubs,
    }, stubs


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ContentDistiller:
    """
    Distills any informational text into a structured entity.

    The AI reads the content, identifies the primary entity, and writes
    a structured entity file. Entity name is extracted automatically.

    Parameters
    ----------
    writer        : EntityWriter
    index         : NameIndex
    model         : str  — default AI model
    file_manifest : FileManifest | None — when provided, provenance is recorded
                    automatically on distillation with a source_path.
    """

    def __init__(
        self,
        writer:        EntityWriter,
        index:         NameIndex,
        model:         str = "auto",
        file_manifest: Optional[FileManifest] = None,
    ) -> None:
        self._writer        = writer
        self._index         = index
        self._model         = model
        self._file_manifest = file_manifest

    async def distill(
        self,
        content:         str,
        model:           Optional[str] = None,
        source:          str = "distilled",
        source_path:     Optional[str] = None,
        source_hash:     Optional[str] = None,
        source_language: Optional[str] = None,
        source_size:     int = 0,
    ) -> dict[str, Any]:
        """
        Distill text into a structured entity.

        Parameters
        ----------
        source_path     : optional path of the file this content came from.
                          When provided, the entity's source_files section is
                          updated and the FileManifest (if configured) is notified.
        source_hash     : SHA-256 hash of the source file ("sha256:<hex>").
        source_language : language slug (e.g. "python"). Auto-detected if omitted.
        source_size     : file size in bytes, for manifest bookkeeping.

        Returns
        -------
        {
          "entity_id":   str,
          "entity_name": str,   # extracted by AI
          "was_created": bool,
          "stub_count":  int,
          "stubs":       list[str],
          "errors":      list[str],
          "source_file": str | None,
        }
        """
        result: dict[str, Any] = {
            "entity_id":   None,
            "entity_name": None,
            "was_created": False,
            "stub_count":  0,
            "stubs":       [],
            "errors":      [],
            "source_file": source_path,
        }

        try:
            caller = get_llm_caller()
            response = await asyncio.to_thread(
                caller,
                prompt=_build_prompt(content),
                system_prompt=_SYSTEM_PROMPT,
                model=model or self._model,
                trace_id=uuid.uuid4().hex,
            )
            raw = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            result["errors"].append(f"AI call failed: {e}")
            logger.error(f"[Distiller] {result['errors'][-1]}")
            return result

        try:
            extracted = _extract_json(raw)
        except Exception as e:
            result["errors"].append(f"JSON parse failed: {e}")
            logger.error(f"[Distiller] {result['errors'][-1]} — raw: {raw[:300]}")
            return result

        entity_name = str(extracted.get("name", "")).strip()
        if not entity_name:
            result["errors"].append("AI did not return an entity name")
            return result

        entity_type = extracted.get("type", "other")
        if entity_type not in VALID_TYPES:
            entity_type = "other"

        try:
            sections, stubs = _map_to_sections(extracted, self._index, self._writer)
        except Exception as e:
            result["errors"].append(f"Mapping failed: {e}")
            return result

        aliases = sections["core"].get("aliases", [])
        existing_id = self._index.get(entity_name)

        if existing_id and self._writer.exists(existing_id):
            entity = self._writer.update(
                existing_id,
                {"type": entity_type, "status": "active", "source": source, "sections": sections},
                merge_sections=True,
            )
            was_created = False
        else:
            entity, was_created = self._writer.create(
                name=entity_name,
                entity_type=entity_type,
                source=source,
                sections_override=sections,
                extra_aliases=aliases,
            )

        result["entity_id"]   = entity["id"]
        result["entity_name"] = entity["name"]
        result["was_created"] = was_created
        result["stub_count"]  = len(stubs)
        result["stubs"]       = stubs

        # Record source provenance if a file path was provided
        if source_path:
            lang      = source_language or detect_language(source_path)
            line_count = content.count("\n") + 1
            sf_entry  = {
                "path":       source_path,
                "lines":      [1, line_count],
                "language":   lang,
                "scanned_at": _now_iso(),
            }
            if source_hash:
                sf_entry["hash"] = source_hash
            if source_size:
                sf_entry["size"] = source_size
            try:
                self._writer.update(entity["id"], {"sections": {"source_files": [sf_entry]}})
            except Exception as exc:
                logger.warning(f"[Distiller] Could not record source provenance for {entity['id']}: {exc}")

            if self._file_manifest is not None:
                try:
                    self._file_manifest.add_entity(
                        path=source_path,
                        entity_id=entity["id"],
                        file_hash=source_hash or "",
                        size=source_size,
                        language=lang,
                    )
                except Exception as exc:
                    logger.warning(f"[Distiller] Could not update FileManifest for {source_path}: {exc}")

        logger.info(
            f"[Distiller] '{entity_name}' → {entity['id']} "
            f"type={entity_type} was_created={was_created} stubs={len(stubs)}"
            + (f" source={source_path}" if source_path else "")
        )
        return result
