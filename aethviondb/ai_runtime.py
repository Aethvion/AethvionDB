"""
core/aethviondb/ai_runtime.py
Pluggable LLM backend for AethvionDB's optional intelligence layer.

The deterministic core — entities, graph, search, validation, snapshots — never
imports this. Only the Layer-2 helpers (distill, expand, deepen) do, and only
through the injected caller below.

The host application wires a backend once via ``set_llm_caller``. The caller has
the same shape as a generic completion call::

    caller(prompt: str, *, system_prompt=None, model=None, trace_id=None, **kw)
        -> response  # an object exposing ``.content`` (or a plain string)

Without an injected caller these features raise ``LLMNotConfiguredError``,
leaving the deterministic core fully usable on its own. This is what keeps
AethvionDB's Layer 1 free of any LLM/provider dependency.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

LLMCaller = Callable[..., Any]


class LLMNotConfiguredError(RuntimeError):
    """Raised when a Layer-2 AI feature runs without an injected LLM backend."""


_llm_caller: Optional[LLMCaller] = None


def set_llm_caller(caller: Optional[LLMCaller]) -> None:
    """Register (or clear, with ``None``) the LLM backend for distill/expand/deepen."""
    global _llm_caller
    _llm_caller = caller


def is_llm_configured() -> bool:
    """True if a backend has been injected (i.e. AI features are available)."""
    return _llm_caller is not None


def get_llm_caller() -> LLMCaller:
    """Return the injected LLM caller, or raise if none is configured."""
    if _llm_caller is None:
        raise LLMNotConfiguredError(
            "AethvionDB AI features (distill / expand / deepen) require an LLM "
            "backend. Inject one with "
            "aethviondb.ai_runtime.set_llm_caller(...). The deterministic "
            "core (entities, graph, search, validation) works without it."
        )
    return _llm_caller


# ── Optional usage logger (embedding/LLM cost tracking) ─────────────────────────
# The host may inject an object with a ``log_api_call(...)`` method to record API
# usage. Absent one, the engine simply doesn't track usage — purely optional.

_usage_logger: Optional[Any] = None


def set_usage_logger(logger_obj: Optional[Any]) -> None:
    """Register (or clear, with ``None``) an optional API-usage logger."""
    global _usage_logger
    _usage_logger = logger_obj


def get_usage_logger() -> Optional[Any]:
    """Return the injected usage logger, or None if the host configured none."""
    return _usage_logger
