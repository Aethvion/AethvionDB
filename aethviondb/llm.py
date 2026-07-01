"""
aethviondb/llm.py
Build an LLM caller from configured provider settings, so the optional
intelligence features (distillation) become enableable from the dashboard —
paste a key in Settings and it works, no restart.

This is Layer 2: the deterministic core never imports it. It bridges the
settings store (provider key) to ``ai_runtime.set_llm_caller``. When no key/SDK
is available, distillation stays gracefully off (a clear 4xx, not a crash).

The caller shape matches ``ai_runtime``:
    caller(prompt, *, system_prompt=None, model=None, trace_id=None, **kw) -> str
"""
from __future__ import annotations

import importlib.util
import os
from typing import Callable, Optional

# Sensible chat defaults per provider; override with AETHVIONDB_LLM_MODEL.
_OPENAI_DEFAULT = "gpt-4o-mini"
_GOOGLE_DEFAULT = "gemini-1.5-flash"


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _openai_caller(api_key: str) -> Callable[..., str]:
    def caller(prompt: str, *, system_prompt: Optional[str] = None,
               model: Optional[str] = None, **_kw) -> str:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=os.getenv("AETHVIONDB_LLM_MODEL") or _OPENAI_DEFAULT,
            messages=messages,
            temperature=0,
        )
        return resp.choices[0].message.content or ""
    return caller


def _google_caller(api_key: str) -> Callable[..., str]:
    def caller(prompt: str, *, system_prompt: Optional[str] = None,
               model: Optional[str] = None, **_kw) -> str:
        from google import genai
        client = genai.Client(api_key=api_key)
        full = (system_prompt + "\n\n" if system_prompt else "") + prompt
        resp = client.models.generate_content(
            model=os.getenv("AETHVIONDB_LLM_MODEL") or _GOOGLE_DEFAULT,
            contents=full,
        )
        return getattr(resp, "text", "") or ""
    return caller


def _provider_from_settings() -> Optional[tuple[Callable[..., str], str]]:
    """Return (caller, provider) for the first configured+installed provider, else None."""
    from .settings_store import get_provider_key
    oai = get_provider_key("openai")
    if oai and _has("openai"):
        return _openai_caller(oai), "openai"
    ggl = get_provider_key("google")
    if ggl and (_has("google.genai") or _has("google.generativeai")):
        return _google_caller(ggl), "google"
    return None


def llm_available() -> bool:
    """True if an LLM caller could be built (key + SDK), or one is already injected."""
    from . import ai_runtime
    return ai_runtime.is_llm_configured() or _provider_from_settings() is not None


def ensure_llm_from_settings() -> bool:
    """Ensure an LLM caller is set. Respects a host-injected caller; otherwise
    builds one from the configured provider key. Returns True if one is active."""
    from . import ai_runtime
    if ai_runtime.is_llm_configured():
        return True
    built = _provider_from_settings()
    if built:
        ai_runtime.set_llm_caller(built[0])
        return True
    return False
