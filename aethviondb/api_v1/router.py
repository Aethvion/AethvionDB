"""
core/aethviondb/api_v1/router.py
Assembles the AethvionDB v1 public API router.

Mounted at /api/v1 in server.py.

URL layout once mounted:
  /api/v1/{db}/raw/*    — live database operations
  /api/v1/{db}/baked/*  — snapshot operations
  /api/v1/{db}/keys/*   — API key management (inside raw_routes)
  /api/v1/              — index (list databases + version info)
"""

from __future__ import annotations

import importlib.util

from fastapi import APIRouter
from pydantic import BaseModel
from aethviondb._utils import get_logger

from .raw_routes   import router as raw_router
from .baked_routes import router as baked_router
from .response import envelope

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["aethviondb-v1"])

# Sub-routers — raw and baked both have /{db}/... paths,
# so they are included directly (no extra prefix needed).
router.include_router(raw_router)
router.include_router(baked_router)


# Discovery endpoint

@router.get("/")
async def api_index():
    """Return API version info and available databases."""
    from aethviondb.config import AETHVIONDB
    from aethviondb.db_registry import list_registered
    import time
    t = time.perf_counter()

    AETHVIONDB.mkdir(parents=True, exist_ok=True)

    # Collect named databases (folders with an entities/ sub-dir)
    seen: set[str] = set()
    databases: list[dict] = []

    for d in sorted(AETHVIONDB.iterdir()):
        if d.is_dir() and d.name != "_db_registry.json" and (d / "entities").exists():
            entity_count = sum(1 for _ in (d / "entities").glob("*.json"))
            databases.append({"name": d.name, "entity_count": entity_count})
            seen.add(d.name)

    # Also include path-based databases registered by the legacy API
    for name, path_str in list_registered().items():
        if name not in seen:
            from pathlib import Path as _P
            entities_dir = _P(path_str) / "entities"
            if entities_dir.exists():
                entity_count = sum(1 for _ in entities_dir.glob("*.json"))
                databases.append({"name": name, "entity_count": entity_count, "path": path_str})

    databases.sort(key=lambda d: d["name"])

    return envelope(
        {
            "version":   "v1",
            "databases": databases,
            "scopes": {
                "raw":   "/{db}/raw/  — live database operations",
                "baked": "/{db}/baked/ — snapshot operations",
                "keys":  "/{db}/keys/ — API key management",
            },
        },
        took_start=t,
    )


# Capabilities + host settings (global, not per-database)

def _has(mod: str) -> bool:
    """True if a module can be imported without importing it."""
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


@router.get("/capabilities")
async def capabilities():
    """Report which optional features are installed and configured, so the
    dashboard can show status and tell the user exactly how to enable each."""
    import time
    t = time.perf_counter()

    from aethviondb.settings_store import get_provider_key
    from aethviondb.ai_runtime import is_llm_configured

    st_local  = _has("sentence_transformers")
    has_oai   = _has("openai")
    has_gg    = _has("google.genai") or _has("google.generativeai")
    key_oai   = bool(get_provider_key("openai"))
    key_gg    = bool(get_provider_key("google"))
    llm       = is_llm_configured()

    caps = [
        {
            "id": "embeddings_local", "name": "Local embeddings", "category": "Embeddings",
            "installed": st_local, "configured": True, "ready": st_local,
            "hint": "" if st_local else "Install the optional dependency to embed offline (free, no API key).",
            "install": 'pip install -e ".[embeddings]"',
        },
        {
            "id": "embeddings_openai", "name": "OpenAI embeddings", "category": "Embeddings",
            "installed": has_oai, "configured": key_oai, "ready": has_oai and key_oai,
            "hint": ("Add an OpenAI API key in Providers below." if has_oai and not key_oai
                     else ("" if has_oai and key_oai else "Install the OpenAI SDK, then add a key.")),
            "install": 'pip install -e ".[openai]"', "needs_key": "openai",
        },
        {
            "id": "embeddings_google", "name": "Google embeddings", "category": "Embeddings",
            "installed": has_gg, "configured": key_gg, "ready": has_gg and key_gg,
            "hint": ("Add a Google API key in Providers below." if has_gg and not key_gg
                     else ("" if has_gg and key_gg else "Install the Google GenAI SDK, then add a key.")),
            "install": 'pip install -e ".[google]"', "needs_key": "google",
        },
        {
            "id": "distillation", "name": "Distillation / expansion", "category": "Intelligence",
            "installed": True, "configured": llm, "ready": llm,
            "hint": "" if llm else "Requires an injected LLM backend (ai_runtime.set_llm_caller); not configurable from the dashboard yet.",
        },
    ]
    return envelope({"capabilities": caps}, took_start=t)


class SettingsPatch(BaseModel):
    providers: dict | None = None
    embedding: dict | None = None


def _mask(key: str) -> dict:
    if not key:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": ("…" + key[-4:]) if len(key) > 4 else "set"}


@router.get("/settings")
async def get_settings():
    """Return host settings with provider keys masked (never the raw key)."""
    import time
    t = time.perf_counter()
    from aethviondb.settings_store import read_settings
    s = read_settings()
    prov = s.get("providers", {})
    return envelope(
        {
            "providers": {
                "openai": _mask((prov.get("openai") or {}).get("api_key", "")),
                "google": _mask((prov.get("google") or {}).get("api_key", "")),
            },
            "embedding": s.get("embedding", {}),
        },
        took_start=t,
    )


@router.put("/settings")
async def put_settings(patch: SettingsPatch):
    """Update host settings (deep-merged). Empty/omitted key fields are ignored,
    so a masked round-trip never wipes a stored key."""
    import time
    t = time.perf_counter()
    from aethviondb.settings_store import write_settings

    body: dict = {}
    if patch.providers:
        provs: dict = {}
        for name in ("openai", "google"):
            block = patch.providers.get(name)
            if isinstance(block, dict):
                k = (block.get("api_key") or "").strip()
                if k:                       # only set when a real key is supplied
                    provs[name] = {"api_key": k}
        if provs:
            body["providers"] = provs
    if patch.embedding:
        body["embedding"] = patch.embedding

    write_settings(body)
    return await get_settings()
