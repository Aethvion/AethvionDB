"""
aethviondb/config.py
Filesystem locations for AethvionDB.

The data directory defaults to ``~/.aethvion/aethviondb`` and can be overridden
with the ``AETHVIONDB_DATA_DIR`` environment variable. Each named database lives
in its own sub-directory ``DATA_DIR/<name>/`` with its own ``entities/`` folder
and ``name_index.json``. (Per-database custom paths are planned.)
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("AETHVIONDB_DATA_DIR") or (Path.home() / ".aethvion" / "aethviondb"))

# Backwards-compatible alias for the name used across the engine.
AETHVIONDB = DATA_DIR
