"""
aethviondb/client.py
A tiny, dependency-free client for talking to an AethvionDB server.

For agents and scripts that use a (possibly remote) AethvionDB over HTTP. Uses
only the standard library, so ``from aethviondb import AethvionClient`` works
anywhere with no extra installs.

    from aethviondb import AethvionClient

    db = AethvionClient(db="shared", actor="coding-agent")   # X-Actor attribution
    db.upsert("PaymentService", type="service",
              summary="Handles checkout.",
              relations=[{"kind": "depends_on", "target_name": "PostgresDB"}])

    for ws in db.search("payment"):
        print(ws["name"])

    for ev in db.watch():                 # live feed, auto-reconnecting
        print(ev["actor"], ev["action"], ev["name"])
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Optional


class AethvionError(RuntimeError):
    """An API call returned an error envelope or a transport failure."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AethvionClient:
    """Synchronous HTTP client for one AethvionDB database."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7475",
        db: str = "default",
        api_key: Optional[str] = None,
        actor: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base = base_url.rstrip("/")
        self.db = db
        self.api_key = api_key
        self.actor = actor
        self.timeout = timeout

    # ── low level ──

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.actor:
            h["X-Actor"] = self.actor
        return h

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode())
                err = j.get("error") or {}
                raise AethvionError(err.get("message") or j.get("detail") or str(e),
                                    status=e.code, code=err.get("code")) from None
            except (ValueError, AttributeError):
                raise AethvionError(str(e), status=e.code) from None
        except urllib.error.URLError as e:
            raise AethvionError(f"Cannot reach AethvionDB at {self.base}: {e.reason}") from None
        if not payload.get("ok", False):
            err = payload.get("error") or {}
            raise AethvionError(err.get("message", "request failed"), code=err.get("code"))
        return payload["data"]

    def _raw(self, path: str) -> str:
        return f"/api/v1/{urllib.parse.quote(self.db)}/raw{path}"

    # ── entities ──

    def upsert(self, name: str, **fields) -> dict:
        """Create or update an entity by name. Extra kwargs: type, kind, status,
        summary, aliases, tags, categories, properties, relations."""
        return self._call("POST", self._raw("/entities/upsert"), {"name": name, **fields})["entity"]

    def get(self, entity_id: str) -> Optional[dict]:
        try:
            return self._call("GET", self._raw(f"/entities/{urllib.parse.quote(entity_id)}"))
        except AethvionError as e:
            if e.status == 404:
                return None
            raise

    def update(self, entity_id: str, mutations: dict, expected_version: Optional[int] = None) -> dict:
        body: dict = {"mutations": mutations}
        if expected_version is not None:
            body["expected_version"] = expected_version
        return self._call("PATCH", self._raw(f"/entities/{urllib.parse.quote(entity_id)}"), body)["entity"]

    def delete(self, entity_id: str, hard: bool = False) -> dict:
        q = "?hard=true" if hard else ""
        return self._call("DELETE", self._raw(f"/entities/{urllib.parse.quote(entity_id)}{q}"))

    def entities(self, status: str = "active", type: Optional[str] = None,
                 kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = {"status": status, "limit": limit}
        if type:
            q["type"] = type
        if kind:
            q["kind"] = kind
        return self._call("GET", self._raw("/entities") + "?" + urllib.parse.urlencode(q))["entities"]

    # ── search / graph ──

    def search(self, query: str, modes: Optional[list[str]] = None,
               filters: Optional[dict] = None, limit: int = 20) -> list[dict]:
        body = {"query": query, "modes": modes or ["keyword"], "filters": filters or {}, "limit": limit}
        return self._call("POST", self._raw("/search"), body)["results"]

    def traverse(self, start_id: str, depth: int = 2, direction: str = "both") -> dict:
        return self._call("POST", self._raw("/graph/traverse"),
                          {"start_id": start_id, "depth": depth, "direction": direction})

    def neighbors(self, entity_id: str, direction: str = "both") -> dict:
        return self._call("GET", self._raw(f"/graph/neighbors/{urllib.parse.quote(entity_id)}?direction={direction}"))

    def path(self, start_id: str, end_id: str, max_depth: int = 6) -> dict:
        return self._call("POST", self._raw("/graph/path"),
                          {"start_id": start_id, "end_id": end_id, "max_depth": max_depth})

    # ── maintenance ──

    def validate(self) -> dict:
        return self._call("GET", self._raw("/validate"))

    def reindex(self) -> dict:
        return self._call("POST", self._raw("/reindex"))

    def backup(self, label: str = "") -> dict:
        return self._call("POST", f"/api/v1/{urllib.parse.quote(self.db)}/backups", {"label": label})

    # ── live feed ──

    def watch(self, last_event_id: Optional[int] = None,
              include_presence: bool = False,
              reconnect_delay: float = 2.0) -> Iterator[dict]:
        """Yield change events from the live feed, reconnecting automatically.

        On a dropped connection it resumes from the last seen sequence id, so
        events emitted during the gap are replayed (no missed writes). Presence
        events are skipped unless ``include_presence`` is set.
        """
        while True:
            q: dict = {}
            if self.api_key:
                q["key"] = self.api_key
            if last_event_id is not None:
                q["last_event_id"] = last_event_id
            url = f"{self.base}/api/v1/{urllib.parse.quote(self.db)}/events"
            if q:
                url += "?" + urllib.parse.urlencode(q)
            try:
                req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=None) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if not line.startswith("data: "):
                            continue
                        try:
                            ev = json.loads(line[6:])
                        except ValueError:
                            continue
                        if "_seq" in ev:
                            last_event_id = ev["_seq"]
                        if ev.get("action") == "presence" and not include_presence:
                            continue
                        yield ev
            except Exception:
                time.sleep(reconnect_delay)   # connection dropped — resume from last_event_id
