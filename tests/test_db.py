"""Case state in Supabase: load/save go over its REST API when configured; files otherwise. No network."""

import httpx

from procurement.cases import db as D
from procurement.cases import flow as F


def test_files_when_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("CASES_DIR", str(tmp_path))
    F.save_state("42", {"decisions": {"a": 1}})
    assert (tmp_path / "42.json").exists() and F.load_state("42") == {"decisions": {"a": 1}}


def test_supabase_when_configured(monkeypatch, tmp_path):
    rows: dict[str, dict] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["apikey"] == "k" and req.url.path == "/rest/v1/case_state"
        if req.method == "POST":
            body = __import__("json").loads(req.content)
            rows[body["uid"]] = body["state"]
            return httpx.Response(201)
        uid = req.url.params["uid"].removeprefix("eq.")
        return httpx.Response(200, json=[{"state": rows[uid]}] if uid in rows else [])

    real = httpx.Client
    monkeypatch.setattr(D.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setenv("CASES_DIR", str(tmp_path))
    assert F.load_state("7") == {"decisions": {}, "replies": []}
    F.save_state("7", {"decisions": {"x": 2}})
    assert F.load_state("7") == {"decisions": {"x": 2}} and not list(tmp_path.iterdir())
