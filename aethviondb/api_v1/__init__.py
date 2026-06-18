"""
core/aethviondb/api_v1
AethvionDB public developer API — version 1.

Mounted at /api/v1 by core/aethviondb/api_v1/router.py.

URL structure:
  /api/v1/{db}/raw/*    → Live fractal database (entities, search, graph, distill)
  /api/v1/{db}/baked/*  → Baked dataset snapshots (list, search, download)
  /api/v1/{db}/keys/*   → API key management

All responses use the standard envelope:
  { "ok": true,  "data": {...}, "meta": {"took_ms": 14.2, "db": "...", "version": "v1"} }
  { "ok": false, "error": {"code": "...", "message": "..."}, "meta": {...} }
"""
