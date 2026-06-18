"""
core/aethviondb/api_v1/response.py
Standard response envelope and cursor codec for the v1 API.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any


# Envelope

def envelope(
    data:       Any,
    db:         str | None   = None,
    took_start: float | None = None,
    cursor:     str | None   = None,
) -> dict:
    """
    Wrap a success response in the standard AethvionDB v1 envelope.

    Parameters
    ----------
    data       : the payload to return under "data"
    db         : database name — included in meta when provided
    took_start : value of time.perf_counter() at request start; computes took_ms
    cursor     : opaque pagination cursor to include in meta
    """
    meta: dict[str, Any] = {"version": "v1"}
    if db is not None:
        meta["db"] = db
    if took_start is not None:
        meta["took_ms"] = round((time.perf_counter() - took_start) * 1000, 2)
    if cursor is not None:
        meta["cursor"] = cursor
    return {"ok": True, "data": data, "meta": meta}


# Cursor codec

def encode_cursor(offset: int) -> str:
    """Encode an offset into a URL-safe, opaque cursor string."""
    return base64.urlsafe_b64encode(
        json.dumps({"offset": offset}).encode()
    ).decode()


def decode_cursor(cursor: str) -> int:
    """Decode a cursor string back to its offset. Returns 0 on any error."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(payload.get("offset", 0))
    except Exception:
        return 0
