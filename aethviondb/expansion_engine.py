"""
core/aethviondb/expansion_engine.py
Autonomous stub-to-entity expansion engine for AethvionDB.

Works in two modes:

1. STUB EXPANSION
   Finds stub entities (status="stub") and generates full content for them
   using AI, given only the entity name and context from related entities.

2. SECTION DEEPENING
   For active entities, turns sub-topics in sections.stubs into their own
   full entities.

Design principles
-----------------
- Never overwrite Layer-1 data with AI-hallucinated corrections.
  The engine ONLY fills missing/empty fields, never replaces existing data.
- Every AI call produces JSON. If the JSON is malformed or missing required
  fields the entity is left as a stub and the error is logged.
- The engine is re-entrant safe: run it multiple times without duplication
  because EntityWriter.create() and the NameIndex deduplicate.

Usage
-----
    from aethviondb.expansion_engine import ExpansionEngine
    engine = ExpansionEngine()

    # Expand one stub
    result = await engine.expand_stub("ws_abc123")

    # Expand up to N stubs autonomously
    report = await engine.run(max_entities=10, model="gemini-1.5-flash")
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from aethviondb._utils import get_logger
from .ai_runtime import get_llm_caller
from .entity_schema import VALID_TYPES
from .entity_writer import EntityWriter
from .name_index import NameIndex, get_index
from .distiller import _extract_json, _map_to_sections as _map_to_entity_sections

logger = get_logger(__name__)


_EXPANSION_SYSTEM_PROMPT = """You are a JSON-only knowledge generator for a structured database.

CRITICAL: Your entire response must be a single valid JSON object. No prose, no markdown, no code fences, no explanation before or after. Start your response with { and end with }.

You will receive an entity name. Generate a comprehensive knowledge entry for it.

Rules:
- "type" must be one of: person, place, event, concept, organization, artifact, creature, substance, process, phenomenon, work, species, universe, other
- "summary" is 2-4 sentences describing what this entity is
- "stubs" lists only important proper nouns that appear in context and deserve their own entries (other people, places, organizations, concepts). Keep this list short — 3-8 items max.
- "timeline" events only when well-known dates are certain
- "properties" captures key structured facts (e.g. birth_year, nationality, field, founded)
- Use empty arrays [] for sections you have nothing to say about
- Do not invent facts. Use known/public knowledge only.

Respond with exactly this JSON shape (no other text):
{"type":"...","aliases":[],"categories":[],"tags":[],"summary":"...","timeline":[{"date":"YYYY","event":"...","ref_names":[]}],"relations":[{"kind":"related_to","target_name":"...","note":""}],"properties":{"key":"value"},"stubs":[]}"""

_EXPANSION_RETRY_SUFFIX = (
    "\n\nIMPORTANT: You must respond with ONLY a JSON object. "
    "No explanation. No markdown. Start immediately with { and end with }."
)


def _build_expansion_prompt(
    entity_name: str,
    context_snippets: list[str],
    max_context_chars: int = 2000,
    retry: bool = False,
    extra_context: Optional[str] = None,
) -> str:
    ctx = "\n".join(context_snippets)[:max_context_chars]
    prompt = f'Generate a knowledge database entry for: "{entity_name}"'
    if ctx:
        prompt += f"\n\nContext from related entities:\n{ctx}"
    if extra_context and extra_context.strip():
        # Extra context from the user (pasted text, file content, etc.)
        # Truncate to 8000 chars so we don't blow up the context window
        ec = extra_context.strip()[:8000]
        prompt += f"\n\nAdditional source material provided by the user — use this as primary reference:\n{ec}"
    prompt += "\n\nRespond with a JSON object only."
    if retry:
        prompt += _EXPANSION_RETRY_SUFFIX
    return prompt


@dataclass
class ExpansionReport:
    """Result of a run() call."""
    expanded:    list[str] = field(default_factory=list)   # entity IDs successfully expanded
    skipped:     list[str] = field(default_factory=list)   # already active
    failed:      list[str] = field(default_factory=list)   # expansion failed
    new_stubs:   list[str] = field(default_factory=list)   # new stub IDs discovered
    total_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "expanded":    self.expanded,
            "skipped":     self.skipped,
            "failed":      self.failed,
            "new_stubs":   self.new_stubs,
            "total_calls": self.total_calls,
            "summary": (
                f"Expanded {len(self.expanded)}, "
                f"skipped {len(self.skipped)}, "
                f"failed {len(self.failed)}, "
                f"created {len(self.new_stubs)} new stubs"
            ),
        }


class ExpansionEngine:
    """
    Autonomous entity expansion engine.

    Parameters
    ----------
    writer : EntityWriter, optional
    model  : str            — default AI model
    concurrency : int       — max parallel AI calls (default 2)
    """

    def __init__(
        self,
        writer: Optional[EntityWriter] = None,
        index: Optional[NameIndex] = None,
        model: str = "auto",
        concurrency: int = 2,
    ) -> None:
        self._writer        = writer or EntityWriter()
        self._index         = index if index is not None else get_index()
        self._default_model = model
        self._semaphore     = asyncio.Semaphore(concurrency)

    # Context gathering

    def _gather_context(self, entity_id: str) -> list[str]:
        """
        Collect summary snippets from related entities to provide
        context for stub expansion.
        """
        entity = self._writer.get(entity_id)
        if not entity:
            return []

        snippets: list[str] = []
        for rel in entity["sections"].get("relations", []):
            if not isinstance(rel, dict):
                continue
            related = self._writer.get(rel.get("target_id", ""))
            if not related:
                continue
            summary = related["sections"]["core"].get("summary", "")
            if summary:
                snippets.append(f"[{rel['kind']}] {related['name']}: {summary}")

        return snippets

    # Core expansion

    async def expand_stub(
        self,
        entity_id: str,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Expand a single stub entity.

        Returns a result dict:
        {
          "entity_id":  "ws_...",
          "success":    bool,
          "new_stubs":  ["name", ...],
          "error":      "..." or None,
        }
        """
        entity = self._writer.get(entity_id)
        result: dict[str, Any] = {
            "entity_id": entity_id,
            "success":   False,
            "new_stubs": [],
            "error":     None,
        }

        if not entity:
            result["error"] = f"Entity {entity_id!r} not found"
            return result

        if entity["sections"]["core"].get("summary"):
            # Entity already has content — skip re-expansion.
            # Self-heal: promote status to "active" if it was incorrectly left as "stub".
            if entity.get("status") != "active":
                self._writer.update(entity_id, {"status": "active"})
            result["success"] = True
            result["error"]   = "already_active"
            return result

        used_model = model or self._default_model
        context    = self._gather_context(entity_id)

        async with self._semaphore:
            from .ai_runtime import get_llm_caller
            caller = get_llm_caller()

            raw = None
            for attempt in range(2):   # up to 2 attempts
                prompt   = _build_expansion_prompt(entity["name"], context, retry=(attempt > 0))
                trace_id = uuid.uuid4().hex
                try:
                    response = await asyncio.to_thread(
                        caller,
                        prompt=prompt,
                        system_prompt=_EXPANSION_SYSTEM_PROMPT,
                        model=used_model,
                        trace_id=trace_id,
                    )
                    raw = response.content if hasattr(response, "content") else str(response)
                except Exception as e:
                    result["error"] = f"AI call failed: {e}"
                    logger.error(f"[ExpansionEngine] {entity_id}: {result['error']}")
                    return result

                try:
                    extracted = _extract_json(raw)
                    break   # success — exit retry loop
                except Exception as parse_err:
                    if attempt == 0:
                        logger.warning(
                            f"[ExpansionEngine] {entity_id}: JSON parse failed on attempt 1, retrying. "
                            f"Raw response ({len(raw)} chars): {raw[:500]!r}"
                        )
                    else:
                        result["error"] = f"JSON parse failed: {parse_err}"
                        logger.error(
                            f"[ExpansionEngine] {entity_id}: {result['error']}. "
                            f"Raw response ({len(raw)} chars): {raw[:500]!r}"
                        )
                        return result
            else:
                # Should not reach here, but guard anyway
                result["error"] = "JSON parse failed after retries"
                return result

        # Map to sections
        try:
            sections, new_stubs = _map_to_entity_sections(extracted, self._index, self._writer)
        except Exception as e:
            result["error"] = f"Section mapping failed: {e}"
            return result

        # Determine entity type
        entity_type = extracted.get("type", entity.get("type", "other"))
        if entity_type not in VALID_TYPES:
            entity_type = entity.get("type", "other")

        # Update: merge new sections into existing entity, set active
        mutations: dict[str, Any] = {
            "type":    entity_type,
            "status":  "active",
            "source":  entity.get("source", "expansion"),
            "sections": sections,
        }
        try:
            self._writer.update(entity_id, mutations, merge_sections=True)
        except Exception as e:
            result["error"] = f"Write failed: {e}"
            return result

        # Create stub entities for newly discovered sub-topics
        new_stub_ids = []
        for stub_name in new_stubs:
            if not self._index.get(stub_name):
                stub_entity, created = self._writer.create(
                    stub_name,
                    entity_type="other",
                    source="expansion",
                )
                if created:
                    self._writer.update(stub_entity["id"], {"status": "stub"})
                    new_stub_ids.append(stub_entity["id"])

        result["success"]   = True
        result["new_stubs"] = new_stub_ids

        logger.info(
            f"[ExpansionEngine] Expanded {entity['name']!r} ({entity_id}) "
            f"type={entity_type}, new_stubs={len(new_stub_ids)}"
        )
        return result

    # Batch run

    async def run(
        self,
        max_entities: int = 20,
        model: Optional[str] = None,
        only_ids: Optional[list[str]] = None,
    ) -> ExpansionReport:
        """
        Expand up to *max_entities* stubs.

        If *only_ids* is given, only those IDs are processed (ignores stub status).
        Otherwise, pulls from the stub queue in the entity store.

        Returns an ExpansionReport.
        """
        report = ExpansionReport()
        used_model = model or self._default_model

        if only_ids:
            targets = only_ids[:max_entities]
        else:
            stubs   = self._writer.list_stubs()
            targets = [s["id"] for s in stubs[:max_entities]]

        if not targets:
            logger.info("[ExpansionEngine] No stubs to expand.")
            return report

        logger.info(f"[ExpansionEngine] Starting expansion of {len(targets)} stubs…")

        tasks = [self.expand_stub(eid, model=used_model) for eid in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            report.total_calls += 1
            if isinstance(res, Exception):
                report.failed.append(str(res))
                continue
            if res["error"] == "already_active":
                report.skipped.append(res["entity_id"])
            elif res["success"]:
                report.expanded.append(res["entity_id"])
                report.new_stubs.extend(res.get("new_stubs", []))
            else:
                report.failed.append(res["entity_id"])

        logger.info(
            f"[ExpansionEngine] Done. "
            f"expanded={len(report.expanded)}, "
            f"failed={len(report.failed)}, "
            f"new_stubs={len(report.new_stubs)}"
        )
        return report

    # Preview (non-destructive)

    async def preview_expand_stub(
        self,
        entity_id:     str,
        model:         Optional[str] = None,
        extra_context: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Run the AI expansion but return the raw proposed data WITHOUT writing
        anything.  The caller can review and then call apply_expand_preview().

        Parameters
        ----------
        extra_context : str, optional
            User-supplied source material (pasted text, file content) that the AI
            should treat as primary reference when generating the expansion.
        """
        entity = self._writer.get(entity_id)
        if not entity:
            return {"entity_id": entity_id, "error": f"Entity {entity_id!r} not found"}

        used_model = model or self._default_model
        context    = self._gather_context(entity_id)

        async with self._semaphore:
            from .ai_runtime import get_llm_caller
            caller = get_llm_caller()

            raw = None
            for attempt in range(2):
                prompt   = _build_expansion_prompt(entity["name"], context, retry=(attempt > 0), extra_context=extra_context)
                trace_id = uuid.uuid4().hex
                try:
                    response = await asyncio.to_thread(
                        caller,
                        prompt=prompt,
                        system_prompt=_EXPANSION_SYSTEM_PROMPT,
                        model=used_model,
                        trace_id=trace_id,
                    )
                    raw = response.content if hasattr(response, "content") else str(response)
                except Exception as e:
                    return {"entity_id": entity_id, "error": f"AI call failed: {e}"}

                try:
                    extracted = _extract_json(raw)
                    break
                except Exception as parse_err:
                    if attempt == 0:
                        logger.warning(f"[ExpansionEngine] preview {entity_id}: JSON parse failed, retrying")
                    else:
                        return {"entity_id": entity_id, "error": f"JSON parse failed: {parse_err}"}
            else:
                return {"entity_id": entity_id, "error": "JSON parse failed after retries"}

        entity_type = extracted.get("type", entity.get("type", "other"))
        if entity_type not in VALID_TYPES:
            entity_type = entity.get("type", "other")
        extracted["type"] = entity_type

        return {
            "entity_id":   entity_id,
            "entity_name": entity["name"],
            "proposed":    extracted,
            "error":       None,
        }

    async def _preview_one_by_name(
        self,
        name:            str,
        context_summary: str,
        model:           Optional[str] = None,
        extra_context:   Optional[str] = None,
    ) -> dict[str, Any]:
        """AI call for a single stub name given a parent-entity context string."""
        used_model = model or self._default_model
        context    = [context_summary] if context_summary else []

        async with self._semaphore:
            from .ai_runtime import get_llm_caller
            caller = get_llm_caller()

            raw = None
            for attempt in range(2):
                prompt   = _build_expansion_prompt(name, context, retry=(attempt > 0), extra_context=extra_context)
                trace_id = uuid.uuid4().hex
                try:
                    response = await asyncio.to_thread(
                        caller,
                        prompt=prompt,
                        system_prompt=_EXPANSION_SYSTEM_PROMPT,
                        model=used_model,
                        trace_id=trace_id,
                    )
                    raw = response.content if hasattr(response, "content") else str(response)
                except Exception as e:
                    return {"name": name, "error": f"AI call failed: {e}"}

                try:
                    extracted = _extract_json(raw)
                    break
                except Exception as parse_err:
                    if attempt == 0:
                        logger.warning(f"[ExpansionEngine] preview '{name}': JSON parse failed, retrying")
                    else:
                        return {"name": name, "error": f"JSON parse failed: {parse_err}"}
            else:
                return {"name": name, "error": "JSON parse failed after retries"}

        entity_type = extracted.get("type", "other")
        if entity_type not in VALID_TYPES:
            entity_type = "other"
        extracted["type"] = entity_type

        return {"name": name, "proposed": extracted, "error": None}

    async def list_expandable_stubs_for(
        self,
        entity_id:         str,
        include_relations: bool = True,
    ) -> dict[str, Any]:
        """
        Return the names/kinds of stubs that could be deepened for an entity,
        WITHOUT running any AI.  Used to populate the selection UI.
        """
        entity = self._writer.get(entity_id)
        if not entity:
            return {"entity_id": entity_id, "error": "Entity not found", "items": []}

        items: list[dict] = []
        seen:  set[str]   = set()

        for name in self._writer.get_stub_names_for(entity_id):
            if name not in seen:
                items.append({"name": name, "kind": "stub"})
                seen.add(name)

        if include_relations:
            relations = (entity.get("sections") or {}).get("relations", [])
            for rel in relations:
                if not isinstance(rel, dict):
                    continue
                target_id = rel.get("target_id", "")
                if not target_id:
                    continue
                related = self._writer.get(target_id)
                if related and related.get("status") == "stub":
                    rel_name = related.get("name", "")
                    if rel_name and rel_name not in seen:
                        items.append({"name": rel_name, "kind": "relation"})
                        seen.add(rel_name)

        return {
            "entity_id":   entity_id,
            "entity_name": entity["name"],
            "items":       items,
        }

    async def preview_deepen_stubs_for(
        self,
        entity_id:         str,
        max_stubs:         int  = 5,
        model:             Optional[str] = None,
        include_relations: bool = True,
        stub_names:        Optional[list[str]] = None,
        extra_context:     Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Preview expanding the sub-topics (stubs) of an active entity.
        Returns proposed AI data for each stub WITHOUT writing anything.

        Parameters
        ----------
        stub_names : list[str] | None
            When provided, use these names directly (skip auto-collection).
        include_relations : bool
            When stub_names is None, also include relation targets that are stubs.
        max_stubs : int
            Cap on auto-collected names (ignored when stub_names is provided).
        extra_context : str, optional
            User-supplied source material that the AI should treat as primary
            reference when generating each expansion.
        """
        entity = self._writer.get(entity_id)
        if not entity:
            return {"entity_id": entity_id, "error": "Entity not found", "previews": []}

        if stub_names is None:
            # Auto-collect ── 1. sub-topic stubs from sections.stubs
            stub_names = list(self._writer.get_stub_names_for(entity_id))

            # 2. Relation targets that are still stub entities
            if include_relations:
                relations = (entity.get("sections") or {}).get("relations", [])
                for rel in relations:
                    if not isinstance(rel, dict):
                        continue
                    target_id = rel.get("target_id", "")
                    if not target_id:
                        continue
                    related = self._writer.get(target_id)
                    if related and related.get("status") == "stub":
                        rel_name = related.get("name", "")
                        if rel_name and rel_name not in stub_names:
                            stub_names.append(rel_name)

            stub_names = stub_names[:max_stubs]

        entity_name = entity["name"]
        summary     = (entity.get("sections") or {}).get("core", {}).get("summary", "")
        ctx_snippet = f"Parent entity: {entity_name}. {summary}"

        coros   = [self._preview_one_by_name(name, ctx_snippet, model, extra_context=extra_context) for name in stub_names]
        results = await asyncio.gather(*coros)

        return {
            "entity_id":          entity_id,
            "entity_name":        entity_name,
            "previews":           list(results),
            "included_relations": include_relations,
        }

    async def apply_expand_preview(
        self,
        entity_id: str,
        proposed:  dict[str, Any],
    ) -> dict[str, Any]:
        """
        Apply a previously previewed expansion to an entity.
        Resolves target_names → target_ids (creating stubs as needed),
        then writes via writer.update(merge_sections=False).
        """
        try:
            sections, new_stubs = _map_to_entity_sections(proposed, self._index, self._writer)
        except Exception as e:
            return {"success": False, "error": f"Section mapping failed: {e}"}

        entity_type = proposed.get("type", "other")
        if entity_type not in VALID_TYPES:
            entity_type = "other"

        mutations: dict[str, Any] = {
            "type":     entity_type,
            "status":   "active",
            "sections": sections,
        }
        try:
            self._writer.update(entity_id, mutations, merge_sections=False)
        except Exception as e:
            return {"success": False, "error": f"Write failed: {e}"}

        # Create stub entities for newly discovered sub-topics
        new_stub_ids = []
        for stub_name in new_stubs:
            if not self._index.get(stub_name):
                stub_entity, created = self._writer.create(
                    stub_name, entity_type="other", source="expansion"
                )
                if created:
                    self._writer.update(stub_entity["id"], {"status": "stub"})
                    new_stub_ids.append(stub_entity["id"])

        return {
            "success":   True,
            "entity_id": entity_id,
            "new_stubs": new_stub_ids,
        }

    async def apply_deepen_previews(
        self,
        entity_id: str,
        previews:  list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Apply a list of previewed stub expansions for an active entity.
        For each preview: create the stub entity (or find existing), then
        apply the proposed expansion to it.
        """
        applied: list[dict] = []
        failed:  list[dict] = []

        for preview in previews:
            name     = preview.get("name", "")
            proposed = preview.get("proposed")
            if not name or not proposed:
                failed.append({"name": name, "error": "Missing name or proposed data"})
                continue

            try:
                stub_entity, created = self._writer.create(
                    name, entity_type="other", source="expansion"
                )
                if created:
                    self._writer.update(stub_entity["id"], {"status": "stub"})
            except Exception as e:
                failed.append({"name": name, "error": f"Create failed: {e}"})
                continue

            result = await self.apply_expand_preview(stub_entity["id"], proposed)
            if result["success"]:
                applied.append({"name": name, "entity_id": stub_entity["id"]})
            else:
                failed.append({"name": name, "error": result.get("error", "Unknown error")})

        return {
            "applied": applied,
            "failed":  failed,
            "total":   len(previews),
        }

    # Section deepening

    async def deepen_stubs_for(
        self,
        entity_id: str,
        max_stubs: int = 5,
        model: Optional[str] = None,
        include_relations: bool = True,
    ) -> ExpansionReport:
        """
        For an active entity, expand its stub sub-topics AND (by default)
        any related entities that are still stubs.

        Parameters
        ----------
        include_relations : bool
            If True (default), relation targets with status='stub' are also
            included in the expansion batch.
        """
        report   = ExpansionReport()
        entity   = self._writer.get(entity_id)
        if not entity:
            return report

        # 1. Sub-topic stubs from sections.stubs (already-existing stubs included)
        stub_names = self._writer.get_stub_names_for(entity_id)
        new_ids:  list[str] = []

        for name in stub_names:
            stub_entity, created = self._writer.create(
                name, entity_type="other", source="expansion"
            )
            if created:
                self._writer.update(stub_entity["id"], {"status": "stub"})
            if stub_entity["id"] not in new_ids:
                new_ids.append(stub_entity["id"])

        # 2. Relation targets that are still stub entities
        if include_relations:
            relations = (entity.get("sections") or {}).get("relations", [])
            for rel in relations:
                if not isinstance(rel, dict):
                    continue
                target_id = rel.get("target_id", "")
                if not target_id or target_id in new_ids:
                    continue
                related = self._writer.get(target_id)
                if related and related.get("status") == "stub":
                    new_ids.append(target_id)

        # Cap at max_stubs
        new_ids = new_ids[:max_stubs]

        if new_ids:
            sub = await self.run(max_entities=max_stubs, model=model, only_ids=new_ids)
            report.expanded    = sub.expanded
            report.failed      = sub.failed
            report.new_stubs   = sub.new_stubs
            report.total_calls = sub.total_calls

        return report
