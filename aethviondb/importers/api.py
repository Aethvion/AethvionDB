"""
aethviondb/importers/api.py
HTTP endpoints for importing external databases.

Provisional surface (prefix /api/import) — expected to change as adapters and a
file-upload flow are added. Kept deliberately small while we test via the UI.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

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


class ScanRequest(BaseModel):
    source_type: str
    folder: str


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@router.get("/sources")
async def sources():
    """List available import source types, each with the file extensions it matches."""
    return {
        "sources": [
            {"type": name, "extensions": list(getattr(cls, "extensions", ()))}
            for name, cls in IMPORTERS.items()
        ]
    }


@router.post("/pick-folder")
async def pick_folder():
    """Open a native folder picker on the server host and return the chosen path.

    Runs in an isolated subprocess so a missing/blocked Tk install can never hang
    the server. Intended for local single-user use (the host and the user are the
    same machine). Falls back gracefully — the caller can still type a path.
    """
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = filedialog.askdirectory(title='Select a folder to scan')\n"
        "print(p or '')\n"
    )
    try:
        proc = await asyncio.to_thread(
            lambda: subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=180,
            )
        )
    except Exception as e:
        raise HTTPException(400, f"Folder picker unavailable on this host: {e}")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return {"folder": lines[-1] if lines else ""}


def _scan(cls, folder: Path) -> list[dict]:
    exts = {e.lower() for e in getattr(cls, "extensions", ())}
    found: list[dict] = []
    for p in folder.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        valid = True
        try:
            cls(str(p))          # constructor validates the file (e.g. SQLite header)
        except Exception:
            valid = False
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        found.append({
            "path":  str(p),
            "name":  p.name,
            "rel":   str(p.relative_to(folder)),
            "size_bytes": size,
            "size_fmt":   _fmt_size(size),
            "valid": valid,
        })
    found.sort(key=lambda f: f["rel"].lower())
    return found


@router.post("/scan")
async def scan(req: ScanRequest):
    """Recursively scan a folder for files matching the source type. No writes."""
    cls = IMPORTERS.get(req.source_type)
    if cls is None:
        raise HTTPException(400, f"Unknown source type {req.source_type!r}. Available: {list(IMPORTERS)}")
    folder = Path(req.folder.strip().strip('"'))
    if not folder.exists():
        raise HTTPException(400, f"No such folder: {folder}")
    if not folder.is_dir():
        raise HTTPException(400, f"Not a folder: {folder}")
    files = await asyncio.to_thread(_scan, cls, folder)
    return {
        "folder": str(folder),
        "source_type": req.source_type,
        "count": len(files),
        "valid_count": sum(1 for f in files if f["valid"]),
        "files": files,
    }


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
