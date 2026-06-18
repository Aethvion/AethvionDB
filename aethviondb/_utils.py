"""
core/aethviondb/_utils.py
Self-contained utilities for the AethvionDB engine.

AethvionDB is being prepared for extraction into a standalone package, so the
engine depends on nothing from the host application. These mirror the small set
of helpers it previously imported from the Suite (logging + atomic JSON I/O),
without any host-specific behaviour (e.g. no trace-ID injection — the host
configures logging handlers).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def get_logger(name: str) -> logging.Logger:
    """Return a standard-library logger; the host application configures handlers."""
    return logging.getLogger(name)


def atomic_json_write(
    path: Union[str, Path],
    data: Union[dict, list],
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> None:
    """Write *data* as JSON to *path* atomically (temp file in same dir + os.replace).

    Prevents a partial/corrupt file if the process is killed mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json(path: Union[str, Path], default: Any = None) -> Any:
    """Load JSON from *path*, returning *default* on missing file or parse error."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
