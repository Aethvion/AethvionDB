"""
tests/test_distill.py
Distillation enablement (P-C): the /raw/distill endpoint builds/uses an LLM
caller; distillation is gracefully off without one, and works with one (mocked
here — no real provider key needed).
"""
from __future__ import annotations

import aethviondb.ai_runtime as ar


def _data(resp):
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("ok") is True, body
    return body["data"]


def test_distill_requires_llm_returns_400(client, db_name, monkeypatch):
    monkeypatch.setattr(ar, "_llm_caller", None)
    monkeypatch.setattr("aethviondb.llm._provider_from_settings", lambda: None)
    r = client.post(f"/api/v1/{db_name}/raw/distill", json={"content": "Some text."})
    assert r.status_code == 400
    assert "LLM" in r.json()["error"]["message"]


def test_distill_with_injected_caller_creates_entity(client, db_name, monkeypatch):
    # A host-injected caller is respected (ensure_llm_from_settings sees it).
    def fake(prompt, **kw):
        return '{"name": "Photosynthesis", "type": "concept", ' \
               '"summary": "Plants convert light into chemical energy."}'
    monkeypatch.setattr(ar, "_llm_caller", fake)

    d = _data(client.post(f"/api/v1/{db_name}/raw/distill",
                          json={"content": "Photosynthesis lets plants make food from sunlight."}))
    assert d["entity_name"] == "Photosynthesis" and d["was_created"] is True

    # the entity really exists
    got = _data(client.get(f"/api/v1/{db_name}/raw/entities/{d['entity_id']}"))
    assert got["type"] == "concept"
    assert "chemical energy" in got["sections"]["core"]["summary"]


def test_distill_bad_llm_output_reports_error(client, db_name, monkeypatch):
    monkeypatch.setattr(ar, "_llm_caller", lambda prompt, **kw: "not json at all")
    r = client.post(f"/api/v1/{db_name}/raw/distill", json={"content": "x"})
    assert r.status_code == 502   # upstream/LLM produced unusable output


def test_capabilities_reports_distillation_ready(client, monkeypatch):
    monkeypatch.setattr("aethviondb.llm.llm_available", lambda: True)
    caps = _data(client.get("/api/v1/capabilities"))["capabilities"]
    dist = next(c for c in caps if c["id"] == "distillation")
    assert dist["ready"] is True and dist["configured"] is True
