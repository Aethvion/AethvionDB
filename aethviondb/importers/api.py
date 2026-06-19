"""
aethviondb/importers/api.py
HTTP endpoints for importing external databases.

Provisional surface (prefix /api/import) — expected to change as adapters and a
file-upload flow are added. Kept deliberately small while we test via the UI.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aethviondb.config import DATA_DIR
from . import IMPORTERS

router = APIRouter(prefix="/api/import", tags=["import"])
_SAFE_DB = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ImportRequest(BaseModel):
    source_type: str   # e.g. "sqlite"
    source: str        # path to the source file on the server
    db: str            # target AethvionDB database name


def _importer(req: ImportRequest):
    cls = IMPORTERS.get(req.source_type)
    if cls is None:
        raise HTTPException(400, f"Unknown source type {req.source_type!r}. Available: {list(IMPORTERS)}")
    try:
        return cls(req.source)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not open source: {e}")


def _db_root(db: str):
    if not _SAFE_DB.match(db):
        raise HTTPException(400, f"Invalid database name {db!r}")
    return DATA_DIR / db


@router.get("/sources")
async def sources():
    """List available import source types."""
    return {"sources": list(IMPORTERS)}


@router.post("/preview")
async def preview(req: ImportRequest):
    """Describe what would be imported — no writes."""
    imp = _importer(req)
    try:
        return await asyncio.to_thread(imp.preview)
    except Exception as e:
        raise HTTPException(400, f"Preview failed: {e}")


@router.post("/run")
async def run(req: ImportRequest):
    """Run the import into the target database. Returns a summary."""
    imp = _importer(req)
    root = _db_root(req.db)
    try:
        summary = await asyncio.to_thread(imp.run, root, req.db)
    except Exception as e:
        raise HTTPException(400, f"Import failed: {e}")
    return summary.__dict__
