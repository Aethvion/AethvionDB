"""
core/aethviondb/vectorizer.py
Generate and store embedding vectors for AethvionDB entities.

Supported providers:
  • Google  — GOOGLE_AI_API_KEY  (google-genai SDK)
  • OpenAI  — OPENAI_API_KEY     (openai SDK)

Vectors are stored inside the entity sections:
  entity["sections"]["vectors"] = {
      "text-embedding-3-small": {
          "embedding":    [...floats...],
          "model":        "text-embedding-3-small",
          "dimensions":   1536,
          "generated_at": "ISO-8601",
          "input":        "first 300 chars of what was embedded"
      }
  }

Persistence: AethvionDB.VECINFO (JSON sidecar in db root)
"""

from __future__ import annotations
from aethviondb._utils import get_logger

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .entity_writer import EntityWriter

logger = get_logger(__name__)

_VEC_INFO_FILE = "AethvionDB.VECINFO"

# In-process task tracking

_vec_tasks: dict[str, asyncio.Task] = {}   # str(db_root) → Task

# Embedding model registry

EMBEDDING_MODELS: dict[str, dict] = {
    # OpenAI
    "text-embedding-3-small": {
        "provider":    "openai",
        "dimensions":  1536,
        "description": "OpenAI text-embedding-3-small — fast, efficient (recommended)",
    },
    "text-embedding-3-large": {
        "provider":    "openai",
        "dimensions":  3072,
        "description": "OpenAI text-embedding-3-large — highest quality",
    },
    "text-embedding-ada-002": {
        "provider":    "openai",
        "dimensions":  1536,
        "description": "OpenAI text-embedding-ada-002 — legacy",
    },
    # Google
    "text-embedding-004": {
        "provider":    "google",
        "dimensions":  768,
        "description": "Gemini text-embedding-004",
    },
    "embedding-001": {
        "provider":    "google",
        "dimensions":  768,
        "description": "Gemini embedding-001 — legacy",
    },
    # Local (sentence-transformers — offline, no API key required)
    "all-MiniLM-L6-v2": {
        "provider":    "local",
        "dimensions":  384,
        "description": "Local — all-MiniLM-L6-v2, fast & efficient (~90 MB, recommended)",
    },
    "all-MiniLM-L12-v2": {
        "provider":    "local",
        "dimensions":  384,
        "description": "Local — all-MiniLM-L12-v2, balanced quality (~120 MB)",
    },
    "all-mpnet-base-v2": {
        "provider":    "local",
        "dimensions":  768,
        "description": "Local — all-mpnet-base-v2, best quality in family (~420 MB)",
    },
    "BAAI/bge-small-en-v1.5": {
        "provider":    "local",
        "dimensions":  384,
        "description": "Local — BAAI/bge-small-en-v1.5, excellent quality/speed (~130 MB)",
    },
    "BAAI/bge-base-en-v1.5": {
        "provider":    "local",
        "dimensions":  768,
        "description": "Local — BAAI/bge-base-en-v1.5, high quality (~440 MB)",
    },
}

# Cost per 1M input tokens (embeddings have no output tokens; local = free)
EMBEDDING_COSTS: dict[str, float] = {
    "text-embedding-3-small":  0.020,
    "text-embedding-3-large":  0.130,
    "text-embedding-ada-002":  0.100,
    "text-embedding-004":      0.025,
    "embedding-001":           0.025,
    # local models have no API cost
    "all-MiniLM-L6-v2":        0.0,
    "all-MiniLM-L12-v2":       0.0,
    "all-mpnet-base-v2":        0.0,
    "BAAI/bge-small-en-v1.5":   0.0,
    "BAAI/bge-base-en-v1.5":    0.0,
}


# State helpers

def is_vectorizing(db_root: Path) -> bool:
    return str(db_root) in _vec_tasks


# Info sidecar

def read_vec_info(db_root: Path) -> dict:
    """Return contents of AethvionDB.VECINFO, or {} if absent / unreadable."""
    p = db_root / _VEC_INFO_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_vec_info(db_root: Path, data: dict) -> None:
    """Persist data to AethvionDB.VECINFO (best-effort — never raises)."""
    try:
        (db_root / _VEC_INFO_FILE).write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"[Vectorizer] Could not write {_VEC_INFO_FILE}: {exc}")


# Text builder

def _entity_to_text(entity: dict) -> str:
    """
    Build a combined text string from entity data for embedding.
    Format: "{name} ({type}). {summary}. Tags: {tags}. Categories: {cats}."
    Empty parts are skipped.
    """
    name    = entity.get("name", "")
    etype   = entity.get("type", "")
    core    = (entity.get("sections") or {}).get("core", {})
    summary = core.get("summary", "")
    tags    = core.get("tags", [])
    cats    = core.get("categories", [])

    parts: list[str] = []

    if name and etype:
        parts.append(f"{name} ({etype}).")
    elif name:
        parts.append(f"{name}.")

    if summary:
        parts.append(f"{summary}.")

    if tags:
        parts.append(f"Tags: {', '.join(tags)}.")

    if cats:
        parts.append(f"Categories: {', '.join(cats)}.")

    return " ".join(parts)


# Provider clients — cached, one instance per process

_client_lock          = threading.Lock()
_google_client:  object = None
_openai_client:  object = None


def _get_google_client():
    """Return a cached google-genai Client (constructed once per process)."""
    global _google_client
    if _google_client is None:
        with _client_lock:
            if _google_client is None:
                from google import genai  # google-genai>=1.0.0
                api_key = os.getenv("GOOGLE_AI_API_KEY", "")
                if not api_key:
                    raise RuntimeError(
                        "GOOGLE_AI_API_KEY is not set. Add it to your .env file."
                    )
                _google_client = genai.Client(api_key=api_key, http_options={"api_version": "v1"})
    return _google_client


def _get_openai_client():
    """Return a cached OpenAI client (constructed once per process)."""
    global _openai_client
    if _openai_client is None:
        with _client_lock:
            if _openai_client is None:
                try:
                    from openai import OpenAI
                except ImportError:
                    raise RuntimeError(
                        "openai package is not installed. Run: pip install openai"
                    )
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    raise RuntimeError(
                        "OPENAI_API_KEY is not set. Add it to your .env file."
                    )
                _openai_client = OpenAI(api_key=api_key)
    return _openai_client


# Local model cache — thread-safe

_local_model_cache: dict[str, object] = {}
_local_model_lock = threading.Lock()


def _get_local_model(model: str):
    """Load and cache a sentence-transformers model (one load per process)."""
    if model not in _local_model_cache:
        with _local_model_lock:
            if model not in _local_model_cache:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError:
                    raise RuntimeError(
                        "sentence-transformers is not installed. "
                        "Run: pip install sentence-transformers"
                    )
                logger.info(f"[Vectorizer] Loading local model {model!r} (first use — may download)…")
                _local_model_cache[model] = SentenceTransformer(model)
                logger.info(f"[Vectorizer] Local model {model!r} ready.")
    return _local_model_cache[model]


# Embedding

def _google_model_id(model: str) -> str:
    """Ensure the model ID has the required 'models/' prefix for the Google API."""
    return model if model.startswith("models/") else f"models/{model}"


async def _embed_google(text: str, model: str) -> list[float]:
    def _sync_embed() -> list[float]:
        client = _get_google_client()
        model_id = _google_model_id(model)
        result = client.models.embed_content(model=model_id, contents=text)
        if not result:
            raise RuntimeError(f"Null response from Google embedding model {model_id!r}")
        # SDK >= 1.x: result.embeddings is a list of ContentEmbedding
        if hasattr(result, "embeddings") and result.embeddings:
            return list(result.embeddings[0].values)
        # Older SDK shape: result.embedding (single ContentEmbedding)
        if hasattr(result, "embedding") and result.embedding:
            return list(result.embedding.values)
        raise RuntimeError(f"Empty embedding response from model {model_id!r}: {result}")
    return await asyncio.to_thread(_sync_embed)


async def _embed_openai(text: str, model: str) -> list[float]:
    def _sync_embed() -> list[float]:
        client = _get_openai_client()
        response = client.embeddings.create(model=model, input=text)
        return response.data[0].embedding
    return await asyncio.to_thread(_sync_embed)


async def _embed_local(text: str, model: str) -> list[float]:
    def _sync() -> list[float]:
        m = _get_local_model(model)
        return m.encode(text, normalize_embeddings=True).tolist()
    return await asyncio.to_thread(_sync)


async def _embed(text: str, model: str) -> list[float]:
    """Route to the correct provider based on EMBEDDING_MODELS registry."""
    provider = EMBEDDING_MODELS.get(model, {}).get("provider", "google")
    if provider == "openai":
        return await _embed_openai(text, model)
    if provider == "local":
        return await _embed_local(text, model)
    return await _embed_google(text, model)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters (GPT tokenizer heuristic)."""
    return max(1, len(text) // 4)


async def _embed_openai_counted(text: str, model: str) -> tuple[list[float], int]:
    """Embed via OpenAI and return (embedding, actual_token_count)."""
    def _sync() -> tuple[list[float], int]:
        client   = _get_openai_client()
        response = client.embeddings.create(model=model, input=text)
        tokens   = getattr(response.usage, "total_tokens", None) or _estimate_tokens(text)
        return response.data[0].embedding, int(tokens)
    return await asyncio.to_thread(_sync)


async def _embed_google_counted(text: str, model: str) -> tuple[list[float], int]:
    """Embed via Google and return (embedding, estimated_token_count).
    Google embedding API does not expose token usage, so we estimate."""
    embedding = await _embed_google(text, model)
    return embedding, _estimate_tokens(text)


async def _embed_local_counted(text: str, model: str) -> tuple[list[float], int]:
    """Embed locally and return (embedding, estimated_token_count). Cost = 0."""
    embedding = await _embed_local(text, model)
    return embedding, _estimate_tokens(text)


async def _embed_counted(text: str, model: str) -> tuple[list[float], int]:
    """Like _embed() but also returns the token count (actual or estimated)."""
    provider = EMBEDDING_MODELS.get(model, {}).get("provider", "google")
    if provider == "openai":
        return await _embed_openai_counted(text, model)
    if provider == "local":
        return await _embed_local_counted(text, model)
    return await _embed_google_counted(text, model)


def _preflight_check(model: str) -> None:
    """Verify the provider client can be constructed. Raises on failure."""
    provider = EMBEDDING_MODELS.get(model, {}).get("provider", "google")
    if provider == "openai":
        _get_openai_client()
    elif provider == "local":
        # Importing is enough — model download happens lazily at first embed call
        try:
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "Run: pip install sentence-transformers"
            )
    else:
        _get_google_client()


# Background vectorization task

async def vectorize_all(
    db_root:       Path,
    writer:        "EntityWriter",
    model:         str,
    force_rewrite: bool = False,
    include_stubs: bool = True,
) -> None:
    """
    Background task: generate embeddings for every non-deleted entity.

    Parameters
    ----------
    force_rewrite : bool
        If True, re-embed entities that already have a vector for this model.
    include_stubs : bool
        If False, stub entities (status == 'stub') are skipped entirely.

    Progress is written to AethvionDB.VECINFO every 5 entities.
    Individual entity failures are logged and counted but do not abort the run.
    """
    key     = str(db_root)
    now_iso = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Mark as running immediately
    write_vec_info(db_root, {
        "status":     "running",
        "model":      model,
        "started_at": now_iso(),
    })

    vectorized      = 0
    skipped         = 0
    failed:         list[str] = []
    first_error:    str | None = None
    session_tokens: int   = 0
    session_cost:   float = 0.0
    cost_per_1m     = EMBEDDING_COSTS.get(model, 0.0)

    # Optional, host-injected usage logger (e.g. for API cost tracking). None
    # unless the host called ai_runtime.set_usage_logger(...).
    from .ai_runtime import get_usage_logger
    _tracker = get_usage_logger()

    try:
        # Preflight: verify the embedding client works before touching any entity
        try:
            _preflight_check(model)
        except Exception as preflight_exc:
            err_msg = str(preflight_exc)
            logger.error(f"[Vectorizer] Preflight failed: {err_msg}")
            write_vec_info(db_root, {
                "status":     "error",
                "error":      err_msg,
                "model":      model,
                "started_at": now_iso(),
            })
            return

        entities = writer.list_all(include_deleted=False)

        # Filter out stubs if requested
        if not include_stubs:
            entities = [e for e in entities if e.get("status") != "stub"]

        total = len(entities)

        write_vec_info(db_root, {
            "status":          "running",
            "model":           model,
            "include_stubs":   include_stubs,
            "started_at":      now_iso(),
            "total":           total,
            "vectorized":      0,
            "skipped":         0,
            "failed":          0,
            "session_tokens":  0,
            "session_cost":    0.0,
        })

        provider = EMBEDDING_MODELS.get(model, {}).get("provider", "google")

        for idx, entity in enumerate(entities):
            entity_id = entity.get("id", "")
            try:
                # Skip if already vectorized and not forcing rewrite
                existing_vec = (
                    (entity.get("sections") or {})
                    .get("vectors", {})
                    .get(model)
                )
                if existing_vec and not force_rewrite:
                    skipped += 1
                else:
                    text               = _entity_to_text(entity)
                    embedding, tokens  = await _embed_counted(text, model)
                    dimensions         = EMBEDDING_MODELS.get(model, {}).get("dimensions", len(embedding))
                    call_cost          = round((tokens / 1_000_000) * cost_per_1m, 8)

                    vec_entry = {
                        "embedding":    embedding,
                        "model":        model,
                        "dimensions":   dimensions,
                        "generated_at": now_iso(),
                        "input":        text[:300],
                    }
                    writer.update(
                        entity_id,
                        {"sections": {"vectors": {model: vec_entry}}},
                        merge_sections=True,
                    )
                    vectorized     += 1
                    session_tokens += tokens
                    session_cost   += call_cost

                    # Log individual embedding call to usage tracker
                    if _tracker:
                        try:
                            import uuid
                            _tracker.log_api_call(
                                provider         = provider,
                                model            = model,
                                prompt           = text[:200],
                                response_content = "",
                                trace_id         = f"embed-{uuid.uuid4().hex[:8]}",
                                operation        = "embedding",
                                success          = True,
                                metadata         = {"usage": {
                                    "prompt_tokens":    tokens,
                                    "completion_tokens": 0,
                                    "total_tokens":     tokens,
                                }},
                                source           = "aethviondb",
                            )
                        except Exception as log_exc:
                            logger.debug(f"[Vectorizer] Usage log failed: {log_exc}")

            except Exception as exc:
                err_str = str(exc)
                logger.warning(f"[Vectorizer] Failed to embed entity {entity_id!r}: {err_str}")
                failed.append(entity_id)
                if first_error is None:
                    first_error = err_str

            # Checkpoint every 5 entities
            if (idx + 1) % 5 == 0:
                write_vec_info(db_root, {
                    "status":          "running",
                    "model":           model,
                    "include_stubs":   include_stubs,
                    "total":           total,
                    "vectorized":      vectorized,
                    "skipped":         skipped,
                    "failed":          len(failed),
                    "last_error":      first_error,
                    "started_at":      now_iso(),
                    "session_tokens":  session_tokens,
                    "session_cost":    round(session_cost, 6),
                })

        # Final status
        final_status = "done" if vectorized > 0 or skipped > 0 else (
            "error" if failed else "done"
        )
        write_vec_info(db_root, {
            "status":          final_status,
            "model":           model,
            "include_stubs":   include_stubs,
            "total":           total,
            "vectorized":      vectorized,
            "skipped":         skipped,
            "failed":          len(failed),
            "failed_ids":      failed,
            "last_error":      first_error,
            "completed_at":    now_iso(),
            "session_tokens":  session_tokens,
            "session_cost":    round(session_cost, 6),
        })
        logger.info(
            f"[Vectorizer] Done: {vectorized} embedded, {skipped} skipped, "
            f"{len(failed)} failed. Tokens: {session_tokens}, Cost: ${session_cost:.6f}"
        )

    except Exception as exc:
        logger.error(f"[Vectorizer] Task level error: {exc}")
        write_vec_info(db_root, {
            "status": "error",
            "error":  str(exc)[:500],
        })
    finally:
        _vec_tasks.pop(key, None)


# Sync embedding (usable from sync node handlers / threads)

def embed_sync(text: str, model: str) -> list[float]:
    """Embed *text* synchronously using the given model.

    Safe to call from a non-async context (e.g. a workflow node handler running
    inside asyncio.to_thread).  Blocks the calling thread until the API responds.
    """
    provider = EMBEDDING_MODELS.get(model, {}).get("provider", "google")
    if provider == "openai":
        response = _get_openai_client().embeddings.create(model=model, input=text)
        return response.data[0].embedding
    if provider == "local":
        return _get_local_model(model).encode(text, normalize_embeddings=True).tolist()
    # google
    model_id = _google_model_id(model)
    result = _get_google_client().models.embed_content(model=model_id, contents=text)
    if not result:
        raise RuntimeError(f"Null response from Google embedding model {model_id!r}")
    if hasattr(result, "embeddings") and result.embeddings:
        return list(result.embeddings[0].values)
    if hasattr(result, "embedding") and result.embedding:
        return list(result.embedding.values)
    raise RuntimeError(f"Empty embedding response from model {model_id!r}: {result}")


# Cancel

def cancel_vectorize(db_root: Path) -> dict:
    """Cancel a running vectorization task and update VECINFO to status=cancelled."""
    key  = str(db_root)
    task = _vec_tasks.get(key)
    if task:
        task.cancel()
    info = read_vec_info(db_root)
    write_vec_info(db_root, {
        **info,
        "status":       "cancelled",
        "cancelled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return {"cancelled": True}
