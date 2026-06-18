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

from fastapi import APIRouter
from aethviondb._utils import get_logger

from .raw_routes   import router as raw_router
from .baked_routes import router as baked_router

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

    from .response import envelope
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
