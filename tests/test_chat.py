"""Ask (chat): tools against made-up stores, and the tool loop with a fake OpenAI client. No network."""

from __future__ import annotations

import json
import shutil
import subprocess
import typing
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from procurement import mailbox
from procurement.cases import flow as F
from procurement.chat import api as chat_api
from procurement.chat import tools as T
from procurement.intake import extract
from procurement.quotes import api as q_api
from procurement.quotes import compare as C
from procurement.quotes import pack
from procurement.quotes.models import Quote, QuoteLine, Term, TenderLine
from procurement.shortlist import score as S
from procurement.vendors import store
from procurement.vendors.store import VendorFields

REAL_CLIENT = chat_api._client

ROWS = [
    ("WP.01", "WATERPROOFING AT TOILET FLOOR"),
    ("WP.02", "WATERPROOFING AT TOILET WALL (UPTO 450 MM HIGHT)"),
    ("WP.02", "WATERPROOFING AT KITCHEN WALL"),  # one code, two meanings
    ("W.P-COBA", "WATERPROOFING WITH BRICK BAT COBA"),
]


def rec(uid, subject, lines):
    return {"uid": uid, "result": {
        "email": {"subject": subject, "sender": "Lakeview Site <site@example.com>", "date": "Mon, 16 Nov 2026",
                  "body": "Dear team, please arrange waterproofing for Tower-1 toilets. Sheet attached.", "attachments": [{"filename": "qty.xlsx"}]},
        "requirement": {"project": {"value": "Lakeview"}},
        "quantities": {"filename": "qty.xlsx", "lines": [{"row": r, "description": t, "total_sqm": q, "uom": "SQM"} for r, t, q in lines]},
        "checks": [],
    }}


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    monkeypatch.setattr(extract, "_client", lambda: pytest.fail("no network calls in tests"))
    monkeypatch.setattr(chat_api, "_client", lambda: pytest.fail("no network calls in tests"))
    wb = Workbook()
    ws = wb.active
    ws.append(["Sl No", "Item Code", "Item Description"])
    for i, (code, desc) in enumerate(ROWS, start=1):
        ws.append([i, code, desc])
    wb.save(tmp_path / "master.xlsx")
    monkeypatch.setenv("ITEM_MASTER_PATH", str(tmp_path / "master.xlsx"))
    inbox = tmp_path / "inbox.json"
    inbox.write_text(json.dumps([rec("1010", "Waterproofing, Tower-1", [(3, "Waterproofing at toilet floor", 120)]),
                                 rec("1011", "Roof coba, Tower-2", [(3, "Waterproofing with brick bat coba", 300)])]))
    monkeypatch.setattr(F, "DEMO_INBOX", inbox)
    monkeypatch.setattr(mailbox, "STORE", tmp_path / "mail")
    monkeypatch.setenv("CASES_DIR", str(tmp_path / "cases"))
    monkeypatch.setenv("VENDORS_PATH", str(tmp_path / "vendors.json"))
    monkeypatch.setenv("VENDORS_PACK", str(tmp_path / "no-pack"))
    monkeypatch.setenv("VENDOR_DEALS_PATH", str(tmp_path / "deals.json"))
    monkeypatch.setenv("VENDOR_DEALS_SEED", str(tmp_path / "no-seed.json"))
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path / "no-quotes"))
    store.create(VendorFields(name="Heron Seal Works", categories=["Waterproofing"], email="heron@example.com", phone="98300 00001", status="approved"))
    store.create(VendorFields(name="Kestrel Coatings", categories=["Waterproofing"]))
    store.create(VendorFields(name="Osprey Cement", categories=["Cement"], phone="98300 00002"))


def call(name, **args):
    out, err = T.run(name, args)
    return json.loads(out), err


# ── Tools ──────────────────────────────────────────────────────────────────────────────────────


def test_list_requirements_with_stage_filter_and_reply_date():
    d, err = call("list_requirements", stage=None)
    assert not err and d["total"] == 2 and {i["uid"] for i in d["items"]} == {"1010", "1011"}
    F.save_state("1010", {"decisions": {}, "replies": [{"to": "s@example.com", "subject": "s", "body": "b", "at": "2026-11-17T05:00:00+00:00", "via": "marked"}]})
    d, _ = call("list_requirements", stage=None)
    row = next(i for i in d["items"] if i["uid"] == "1010")
    assert row["last_reply_to_site_at"] == "2026-11-17T05:00:00+00:00" and row["replies_to_site"] == 1
    d, _ = call("list_requirements", stage="rfq_sent")
    assert d["total"] == 0 and d["items"] == []


def test_get_requirement_and_read_email():
    d, err = call("get_requirement", uid="1010")
    assert not err and d["uid"] == "1010" and d["lines"][0]["match"]["code"] == "WP.01"
    assert "draft" not in d and "email" not in d  # trimmed; read_email has the text
    d, err = call("get_requirement", uid="case 1010")
    assert not err and d["uid"] == "1010"
    d, err = call("read_email", uid="1011")
    assert not err and d["subject"] == "Roof coba, Tower-2" and "waterproofing" in d["body"] and d["attachments"] == ["qty.xlsx"]
    d, err = call("get_requirement", uid="9999")
    assert err and "9999" in d["error"]
    d, err = call("read_email", uid="../x")
    assert err


def test_item_code_search_and_issues():
    d, err = call("search_item_codes", query="WP.02")
    assert not err and {i["code"] for i in d["items"]} == {"WP.02"} and d["total"] == 2
    assert all("same_code_different_text" in i["issues"] for i in d["items"])
    d, _ = call("search_item_codes", query="toilet wall")
    assert d["items"][0]["code"] == "WP.02"
    d, err = call("item_code_issues")
    assert not err and d["counts"]["same_code_different_text"] == 1
    assert any(g["codes"] == ["WP.02"] for g in d["groups"]["items"])


def test_item_master_missing_is_a_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ITEM_MASTER_PATH", str(tmp_path / "nope.xlsx"))
    d, err = call("item_code_issues")
    assert err and "item master" in d["error"]
    d, err = call("list_requirements", stage=None)
    assert err


def test_vendors_missing_contact_and_shortlist():
    d, err = call("search_vendors", query=None, category="Waterproofing", missing_contact_only=True)
    assert not err and [v["name"] for v in d["items"]] == ["Kestrel Coatings"]
    assert set(d["items"][0]["missing"]) >= {"email", "phone"}
    d, _ = call("search_vendors", query="heron", category=None, missing_contact_only=None)
    assert d["total"] == 1 and d["items"][0]["has_contact"]
    assert {c["name"] for c in d["categories"]} == {"Waterproofing", "Cement"}
    d, err = call("vendor_shortlist", category="Waterproofing")
    assert not err and {v["name"] for v in d["vendors"]["items"]} == {"Heron Seal Works", "Kestrel Coatings"}


def _quotes(monkeypatch):
    tender = [TenderLine(sl_no=1, code="WP.01", description="FLOOR", unit="sqft", qty=1000.0),
              TenderLine(sl_no=2, code="WP.02", description="WALL", unit="sqft", qty=500.0)]

    def q(vendor, rate, gst):
        lines = [QuoteLine(sl_no=s, description=f"LINE {s}", unit="sqft", unit_as_written="SQFT", qty=t.qty, rate=rate, amount=None, page=1)
                 for s, t in ((1, tender[0]), (2, tender[1]))]
        return Quote(vendor=vendor, file=f"Quotation_{vendor}.pdf", lines=lines, read_confidence="high",
                     terms=[Term(key="gst", value=gst, page=2, text=f"GST {gst}")])

    quotes = [q("Heron Seal Works", 40.0, "18% extra"), q("Kestrel Coatings", 38.0, "inclusive")]
    monkeypatch.setattr(q_api, "_missing", lambda: None)
    monkeypatch.setattr(pack, "read_quotes", lambda: [x.model_copy(deep=True) for x in quotes])
    monkeypatch.setattr(pack, "comparison", lambda: C.compare(tender, [x.model_copy(deep=True) for x in quotes]))


def test_quotes_and_comparison(monkeypatch):
    d, err = call("list_quotes", vendor=None)
    assert err and "No quotation" in d["error"]  # nothing in the pack folder
    _quotes(monkeypatch)
    d, err = call("list_quotes", vendor="kestrel")
    assert not err and d["total"] == 1 and d["items"][0]["terms"][0]["value"] == "inclusive"
    assert d["items"][0]["lines"][1]["code"] == "WP.02" and d["items"][0]["quantity_basis"] == "tender"
    d, err = call("list_quotes", vendor="nobody")
    assert err and "Heron Seal Works" in d["error"]
    d, err = call("quote_comparison", basis="tender")
    assert not err and d["vendors"][0]["vendor"] == "Kestrel Coatings" and d["vendors"][0]["rank_repriced"] == 1
    assert d["vendors"][0]["repriced_total_inr"] == "₹57,000"


def test_unknown_tool_and_bad_args():
    d, err = call("drop_tables")
    assert err and "No tool" in d["error"]
    out, err = T.run("search_item_codes", {"query": ""})
    assert err


def test_schemas_are_strict():
    for t in T.openai_tools():
        p = t["parameters"]
        assert t["strict"] and p["additionalProperties"] is False and sorted(p["required"]) == sorted(p["properties"])


def test_stages_cover_every_case_stage_and_the_shortlist_names_every_factor():
    assert T.STAGES == [*typing.get_args(F.Stage), "error"]
    lr = T.BY_NAME["list_requirements"]
    assert lr["parameters"]["properties"]["stage"]["enum"] == [*T.STAGES, None]
    assert all(s in lr["description"] for s in T.STAGES)
    desc = T.BY_NAME["vendor_shortlist"]["description"].lower()
    assert all(S.LABELS[k].lower() in desc for k in S.WEIGHTS)


# ── The loop, with a fake client ───────────────────────────────────────────────────────────────


def fn_call(name, args, cid="c1"):
    return NS(type="function_call", name=name, arguments=json.dumps(args), call_id=cid,
              to_dict=lambda: {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": cid})


def text_resp(text):
    return NS(output=[NS(type="message", content=[NS(type="output_text", text=text)])], output_text=text, status="completed")


class FakeClient:
    def __init__(self, responses):
        self.responses_q = list(responses)
        self.calls: list[dict] = []
        self.responses = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        r = self.responses_q.pop(0) if len(self.responses_q) > 1 else self.responses_q[0]
        return r() if callable(r) else r


def body(q, page=None):
    return chat_api.ChatIn(messages=[chat_api.Turn(role="user", content=q)], page=page)


def test_loop_runs_tools_then_answers():
    fake = FakeClient([NS(output=[fn_call("list_requirements", {"stage": None})], output_text="", status="completed"),
                       text_resp("Two requirements: **1010** and **1011**.")])
    out = chat_api.answer(body("What's open?", chat_api.Page(hash="#cases/1010", uid="1010")), client=fake)
    assert out["reply"].startswith("Two requirements") and out["rounds"] == 1
    assert out["tool_calls"] == [{"name": "list_requirements", "input_summary": "list_requirements", "error": False}]
    second = fake.calls[1]["input"]
    assert second[-1]["type"] == "function_call_output" and json.loads(second[-1]["output"])["total"] == 2
    assert "requirement 1010" in fake.calls[0]["input"][0]["content"]  # page context in the user turn, not the system prompt
    assert fake.calls[0]["instructions"] == chat_api.SYSTEM and fake.calls[0]["store"] is False


def test_loop_stops_at_the_cap():
    fake = FakeClient([NS(output=[fn_call("item_code_issues", {})], output_text="", status="completed")])
    out = chat_api.answer(body("Loop forever"), client=fake)
    assert len(fake.calls) == chat_api.MAX_ROUNDS + 1
    assert fake.calls[-1]["tool_choice"] == "none" and all(c["tool_choice"] == "auto" for c in fake.calls[:-1])
    assert len(out["tool_calls"]) == chat_api.MAX_ROUNDS and "narrower question" in out["reply"]


def test_refusal_is_a_polite_reply():
    fake = FakeClient([NS(output=[NS(type="message", content=[NS(type="refusal", refusal="no")])], output_text="", status="completed")])
    out = chat_api.answer(body("something odd"), client=fake)
    assert out["refused"] and "can't help" in out["reply"]


def test_history_is_capped_and_must_end_with_a_question():
    turns = [chat_api.Turn(role="user" if i % 2 == 0 else "assistant", content=f"t{i}") for i in range(45)]
    items = chat_api.build_input(turns, None)
    assert len(items) <= chat_api.MAX_TURNS and items[0]["role"] == "user" and items[-1]["content"].endswith("t44")
    with pytest.raises(chat_api.ChatError):
        chat_api.build_input([chat_api.Turn(role="user", content="q"), chat_api.Turn(role="assistant", content="a")], None)


def test_no_key_returns_503(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(chat_api, "_client", REAL_CLIENT)  # the real one: it must refuse before any network call
    app = FastAPI()
    app.include_router(chat_api.router)
    c = TestClient(app)
    s = c.get("/api/chat/status").json()
    assert s["ready"] is False and "OPENAI_API_KEY" in s["message"]
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and r.json()["detail"] == chat_api.NO_KEY


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_markdown_renderer_escapes_html():
    from pathlib import Path

    js = Path(chat_api.__file__).resolve().parent.parent / "web" / "chat.js"
    script = f"""
      global.window = {{}}; global.document = {{readyState: "loading", addEventListener() {{}}, getElementById() {{ return null; }}}};
      global.sessionStorage = {{getItem() {{ return null; }}, setItem() {{}}}}; global.location = {{hash: ""}};
      require({json.dumps(str(js))});
      const md = window.askMarkdown;
      console.log(JSON.stringify([
        md('<img src=x onerror=alert(1)> **bold**'),
        md('| Vendor | Total |\\n|---|---|\\n| A <b> | ₹57,000 |'),
        md('- one\\n- two'),
      ]));
    """
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert "<img" not in out[0] and "&lt;img" in out[0] and "<b>bold</b>" in out[0]
    assert "<table>" in out[1] and "A &lt;b&gt;" in out[1] and '<td class="n">₹57,000</td>' in out[1]
    assert out[2] == "<ul><li>one</li><li>two</li></ul>"
