"""
core/aethviondb/api_v1/auth.py
API key management for the AethvionDB v1 developer API.

Keys are stored per-database in  <db_root>/api_keys.json.
Only SHA-256 hashes of keys are persisted — the raw key is returned once
at generation time and never stored.

If no keys are configured for a database, all requests pass through
(open-access / localhost dev mode).

Authentication headers accepted (checked in this priority order):
  X-AethvionDB-Key: <key>
  Authorization: Bearer <key>
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from typing import Optional

_KEYS_FILE = "api_keys.json"


# Storage helpers

def _keys_path(db_root: Path) -> Path:
    return db_root / _KEYS_FILE


def _load_raw(db_root: Path) -> list[dict]:
    p = _keys_path(db_root)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_raw(db_root: Path, keys: list[dict]) -> None:
    db_root.mkdir(parents=True, exist_ok=True)
    _keys_path(db_root).write_text(
        json.dumps(keys, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# Public API

def has_keys(db_root: Path) -> bool:
    """True when at least one key is registered for this database."""
    return bool(_load_raw(db_root))


def generate_key(
    db_root: Path,
    label:  str       = "default",
    scopes: list[str] | None = None,
) -> str:
    """
    Generate a new API key, store its hash, and return the raw key.
    The raw key is returned exactly once — never stored.
    Raises ValueError if a key with this label already exists.
    """
    existing = _load_raw(db_root)
    if any(k["label"] == label for k in existing):
        raise ValueError(f"A key with label {label!r} already exists.")

    raw = "adb_" + secrets.token_urlsafe(32)
    existing.append({
        "hash":    _hash(raw),
        "label":   label,
        "scopes":  scopes or ["read", "write"],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save_raw(db_root, existing)
    return raw  # Only time the raw key is ever exposed


def validate_key(db_root: Path, key: str) -> bool:
    """Return True if the key is valid for this database."""
    h = _hash(key)
    return any(k["hash"] == h for k in _load_raw(db_root))


def list_keys(db_root: Path) -> list[dict]:
    """Return public metadata for all keys (no hashes, no raw keys)."""
    return [
        {"label": k["label"], "scopes": k["scopes"], "created": k["created"]}
        for k in _load_raw(db_root)
    ]


def revoke_key(db_root: Path, label: str) -> bool:
    """Remove a key by label. Returns True if found and removed."""
    keys = _load_raw(db_root)
    new_keys = [k for k in keys if k["label"] != label]
    if len(new_keys) == len(keys):
        return False
    _save_raw(db_root, new_keys)
    return True


# FastAPI dependency

def check_auth(
    db_root:            Path,
    authorization:      Optional[str] = None,
    x_aethviondb_key:   Optional[str] = None,
) -> None:
    """
    Validate the request's API key against the database's key store.
    No-op when no keys are configured (open-access mode).
    Raises HTTP 401 on invalid / missing key.
    """
    if not has_keys(db_root):
        return  # No keys = open access

    key = x_aethviondb_key
    if not key and authorization and authorization.startswith("Bearer "):
        key = authorization[7:].strip()

    if not key or not validate_key(db_root, key):
        raise HTTPException(
            status_code=401,
            detail={
                "code":    "UNAUTHORIZED",
                "message": (
                    "Invalid or missing API key. "
                    "Use  Authorization: Bearer <key>  or  X-AethvionDB-Key: <key>."
                ),
            },
        )
