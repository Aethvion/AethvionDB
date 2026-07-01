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
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from aethviondb._utils import get_logger

from .raw_routes   import router as raw_router
from .baked_routes import router as baked_router
from .response import envelope

_SAFE_DB = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

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
    from aethviondb.llm import llm_available
    llm       = llm_available()   # host-injected caller OR a configured provider key + SDK

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
            "id": "distillation", "name": "Distillation (text → entity)", "category": "Intelligence",
            "installed": has_oai or has_gg, "configured": llm, "ready": llm,
            "hint": ("" if llm else
                     ("Add an OpenAI or Google API key in Providers below."
                      if (has_oai or has_gg) else
                      "Install a provider SDK (openai/google) and add a key to enable distillation.")),
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


# Database management (create / delete / rename) — operates on folders under the
# data dir, with a safety guard so it never touches a path outside it.

class DbCreate(BaseModel):
    name: str


class DbRename(BaseModel):
    new_name: str


def _data_dir() -> Path:
    from aethviondb.config import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _safe_db_root(name: str) -> Path:
    """Resolve a managed database folder, refusing names that escape the data dir."""
    if not _SAFE_DB.match(name):
        raise HTTPException(400, f"Invalid database name {name!r}.")
    root = (_data_dir() / name).resolve()
    if root.parent != _data_dir().resolve():
        raise HTTPException(400, "Refusing to manage a database outside the data directory.")
    return root


@router.post("/databases")
async def create_database(req: DbCreate):
    """Create an empty database (folder under the data dir)."""
    t = time.perf_counter()
    root = _safe_db_root(req.name)
    if (root / "entities").exists():
        raise HTTPException(409, f"Database {req.name!r} already exists.")
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "chunks").mkdir(parents=True, exist_ok=True)
    return envelope({"name": req.name, "created": True}, took_start=t)


@router.delete("/databases/{name}")
async def delete_database(name: str):
    """Permanently delete a database and all its data."""
    t = time.perf_counter()
    root = _safe_db_root(name)
    if not root.exists():
        raise HTTPException(404, f"Database {name!r} not found.")
    from aethviondb import snapshot
    snapshot.invalidate(root)                     # drop caches before removing files
    shutil.rmtree(root, ignore_errors=True)
    try:
        from aethviondb.db_registry import remove_db
        remove_db(name)
    except Exception:
        pass
    return envelope({"deleted": name}, took_start=t)


@router.post("/databases/{name}/rename")
async def rename_database(name: str, req: DbRename):
    """Rename a database (moves its folder)."""
    t = time.perf_counter()
    src = _safe_db_root(name)
    dst = _safe_db_root(req.new_name)
    if not src.exists():
        raise HTTPException(404, f"Database {name!r} not found.")
    if dst.exists():
        raise HTTPException(409, f"Database {req.new_name!r} already exists.")
    from aethviondb import snapshot
    snapshot.invalidate(src)
    shutil.move(str(src), str(dst))
    return envelope({"renamed": True, "old_name": name, "new_name": req.new_name}, took_start=t)
