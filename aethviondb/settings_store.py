"""
aethviondb/settings_store.py
Host-side settings persisted to DATA_DIR/settings.json.

Holds the things a user can enable from the dashboard without touching a shell or
restarting the server: optional provider API keys, the default embedding model,
and feature toggles. Provider keys are resolved settings-first, then the
environment, so an existing ``OPENAI_API_KEY`` still works untouched.

Trust model: keys live in plaintext on the host — the same trust level as a
``.env`` file. This is a local, single-user oriented store; the API masks keys on
read and never returns them in full. When the multiplayer/commercial mode lands,
this is the seam to move secrets into a real secret store.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from aethviondb._utils import get_logger
from aethviondb.config import DATA_DIR

logger = get_logger(__name__)

_LOCK = threading.Lock()
_FILE = "settings.json"

# Environment fallbacks per provider (kept here so the mapping has one home).
_ENV_KEYS = {"openai": "OPENAI_API_KEY", "google": "GOOGLE_AI_API_KEY"}

_DEFAULTS: dict = {
    "providers": {
        "openai": {"api_key": ""},
        "google": {"api_key": ""},
    },
    "embedding": {"default_model": "all-MiniLM-L6-v2"},
}


def _path() -> Path:
    return DATA_DIR / _FILE


def _deep_update(base: dict, incoming: dict) -> dict:
    for k, v in (incoming or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def read_settings() -> dict:
    """Return the full settings dict, with defaults filled in for any gaps."""
    data: dict = {}
    p = _path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[settings] Could not read {p}: {exc}")
            data = {}
    merged = json.loads(json.dumps(_DEFAULTS))  # deep copy of defaults
    return _deep_update(merged, data)


def write_settings(patch: dict) -> dict:
    """Deep-merge *patch* into the stored settings and persist. Returns the result."""
    with _LOCK:
        current = read_settings()
        _deep_update(current, patch or {})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(_path(), 0o600)   # best-effort: owner read/write only
        except Exception:
            pass
        return current


def get_provider_key(provider: str) -> str:
    """Resolve a provider API key: stored settings first, then the environment."""
    s = read_settings()
    key = ((s.get("providers") or {}).get(provider) or {}).get("api_key", "")
    if key:
        return key
    env = _ENV_KEYS.get(provider)
    return os.getenv(env, "") if env else ""
