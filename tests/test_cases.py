"""Requirement -> item codes -> reply / vendors. Made-up master and emails, no network."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from procurement import mailbox
from procurement.cases import api as cases_api
from procurement.cases import flow as F
from procurement.cases import reply as R
from procurement.intake import extract
from procurement.itemcodes import master as M
from procurement.shortlist import score as S

ROWS = [
    ("WP.01", "WATERPROOFING AT TOILET FLOOR"),
    ("WP.02", "WATERPROOFING AT TOILET WALL (UPTO 450 MM HIGHT)"),
    ("W.P-COBA", "WATERPROOFING WITH BRICK BAT COBA"),
    ("WP.MR", "Scaffolding charges"),
]


def sheet_line(row, text, qty=None, code=None):
    return {"row": row, "description": text, "total_sqm": qty, "uom": "SQM" if qty else None, "code": code}


def rec(uid, subject, lines, checks=()):
    return {"uid": uid, "result": {
        "email": {"subject": subject, "sender": "Riverside Site <site@example.com>", "date": "Mon, 16 Nov 2026"},
        "requirement": {"project": {"value": "Riverside"}},
        "quantities": {"lines": lines},
        "checks": list(checks),
    }}


BLOCK = {"id": "sheet", "severity": "block", "title": "The quantity sheet is missing", "detail": "Ask site to resend it.", "source": "x"}
WARN = {"id": "basics", "severity": "warn", "title": "Some basics aren't in the email", "detail": "Not stated: finish date.", "source": "x"}

RECORDS = [
    rec("1", "Waterproofing, Tower-1", [
        sheet_line(3, "Waterproofing at toilet floor", 120),
        sheet_line(4, "Waterproofing at toilets", 80),
        sheet_line(5, "OPC 53 cement bags"),
        sheet_line(6, "Waterproofing at toilet floor", 10, code="WP.MR"),
    ], checks=[WARN]),
    rec("2", "Roof coba, Tower-2", [sheet_line(3, "Waterproofing with brick bat coba", 300)]),
    rec("3", "Toilet floor, Tower-3", [sheet_line(3, "Waterproofing at toilet floor", 50)], checks=[BLOCK]),
]


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    monkeypatch.setattr(extract, "_client", lambda: pytest.fail("no network calls in tests"))
    wb = Workbook()
    ws = wb.active
    ws.append(["ERP ITEM CODE for Water-proofing work", None, None])
    ws.append(["Sl No", "Item Code", "Item Description"])
    for i, (code, desc) in enumerate(ROWS, start=1):
        ws.append([i, code, desc])
    wb.save(tmp_path / "master.xlsx")
    monkeypatch.setenv("ITEM_MASTER_PATH", str(tmp_path / "master.xlsx"))
    inbox = tmp_path / "inbox.json"
    inbox.write_text(json.dumps(RECORDS))
    monkeypatch.setattr(F, "DEMO_INBOX", inbox)
    monkeypatch.setattr(mailbox, "STORE", tmp_path / "mail")
    monkeypatch.setenv("CASES_DIR", str(tmp_path / "cases"))
    for k in ("REPLY_ALLOWED_TO", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


def line(case, row):
    return next(ln for ln in case.lines if ln.id.endswith(f"-r{row}"))


def test_lines_sorted_into_ready_and_to_reply():
    c = F.build_case("1")
    assert line(c, 3).status == "ready" and line(c, 3).codes == ["WP.01"]
    assert line(c, 3).category == "Waterproofing"
    toilets = line(c, 4)
    assert toilets.status == "to_reply" and [i.kind for i in toilets.issues] == ["partial"]
    assert [i.kind for i in line(c, 5).issues] == ["no_match"]
    assert line(c, 5).category == "Cement"
    assert "conflict" in [i.kind for i in line(c, 6).issues]  # site sheet says WP.MR = scaffolding
    assert c.stage == "item_codes"
    assert c.counts["ready"] == 1 and c.counts["to_reply"] == 3


def test_all_exact_is_ready_to_approve():
    c = F.build_case("2")
    assert c.stage == "approve" and c.counts["ready"] == 1
    assert R.draft(c).needed is False


def test_reply_lists_every_open_question_and_check():
    d = R.draft(F.build_case("1"))
    assert d.needed and d.subject == "Re: Waterproofing, Tower-1" and d.to.endswith("<site@example.com>")
    assert "Waterproofing at toilets (80 SQM)" in d.body
    assert "WP.01 and WP.02" in d.body and "floor, wall or both" in d.body  # floor/wall split asked plainly
    assert "We have no item code for this" in d.body and "OPC 53 cement bags" in d.body
    assert "The item code is unclear" in d.body
    assert "Not stated: finish date." in d.body
    assert "The other 1 line" in d.body
    assert len(d.lines) == 3


def test_picking_a_code_settles_the_line_and_drops_it_from_the_reply():
    c = F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"])
    assert line(c, 4).status == "decided" and line(c, 4).codes == ["WP.01", "WP.02"]
    assert "Waterproofing at toilets" not in R.draft(c).body
    c = F.decide("1", "1-r4", "clear")
    assert line(c, 4).status == "to_reply"


def test_new_code_suggestion_can_be_used_but_not_a_made_up_code():
    c = F.build_case("1")
    sugg = line(c, 5).match.suggestion.code
    assert line(F.decide("1", "1-r5", "use_code", [sugg]), 5).codes == [sugg]
    with pytest.raises(F.CaseError):
        F.decide("1", "1-r5", "use_code", ["ZZ.99"])
    with pytest.raises(F.CaseError):
        F.decide("1", "1-r5", "use_code", [])


def test_settling_every_line_moves_to_approve():
    F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"])
    F.decide("1", "1-r5", "skip")
    c = F.decide("1", "1-r6", "use_code", ["WP.01"])
    assert c.stage == "approve"
    assert c.counts["skipped"] == 1


def test_ask_site_holds_a_ready_line():
    c = F.decide("2", "2-r3", "ask_site", note="Is the coba thickness 100 mm?")
    assert c.stage == "item_codes"
    assert "Is the coba thickness 100 mm?" in R.draft(c).body


def test_blocking_email_check_holds_vendors_until_sorted():
    c = F.build_case("3")
    assert c.counts["ready"] == 1 and c.stage == "item_codes"
    assert "quantity sheet is missing" in R.draft(c).body
    c = F.resolve_check("3", "sheet", True)
    assert c.stage == "approve"
    assert R.draft(c).needed is False
    with pytest.raises(F.CaseError):
        F.resolve_check("3", "nope", True)


def test_marking_a_reply_sent_moves_to_waiting_on_site():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    c = F.build_case("1")
    assert c.stage == "waiting_on_site" and c.replies[0].via == "marked"


def test_sending_only_to_test_inboxes(monkeypatch):
    with pytest.raises(R.SendError) as e:
        R.send("1", "site@example.com", "Re: x", "body")
    assert e.value.status == 403
    monkeypatch.setenv("REPLY_ALLOWED_TO", "@test.example")
    with pytest.raises(R.SendError) as e:
        R.send("1", "qa@test.example", "Re: x", "body")
    assert e.value.status == 409  # no Gmail set up

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == "smtp.gmail.com" and port == 465

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            assert p == "abcd"

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setenv("GMAIL_ADDRESS", "desk@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "ab cd")
    r = R.send("1", "QA <qa@test.example>", "Re: x", "hello", smtp_factory=FakeSMTP)
    assert r.via == "smtp" and r.to == "qa@test.example"
    assert sent[0]["To"] == "qa@test.example" and sent[0].get_content().strip() == "hello"
    with pytest.raises(R.SendError):
        R.send("1", "other@example.com", "Re: x", "hello", smtp_factory=FakeSMTP)


@pytest.fixture
def vendors(monkeypatch):
    """Three made-up vendors and a fake scorer, so the real directory and deals are never read."""
    from procurement.vendors import store as V

    vs = [V.Vendor(id=f"v{i}", name=f"Vendor {i}", categories=["Waterproofing"], email=f"v{i}@vendor.example" if i != 3 else "", created_at="2026-11-21", updated_at="2026-11-21")
          for i in (1, 2, 3)]
    monkeypatch.setattr(V, "load_all", lambda: vs)
    calls = []

    def fake(category, codes):
        calls.append((category, codes))
        scored = [S.Scored(vendor_id=v.id, name=v.name, status=v.status, categories=v.categories, score=90 - 10 * i,
                           coverage=0.5, rank=i + 1, selected=i < 2, factors=[]) for i, v in enumerate(vs)]
        return S.Shortlist(category=category, item_codes=codes, medians={}, weights={}, vendors=scored, excluded=[], selected=2, notes=[])

    monkeypatch.setattr(S, "shortlist", fake)
    return calls


def test_approve_only_when_every_line_is_settled(vendors):
    with pytest.raises(F.CaseError, match="still need an item code"):
        F.approve("1")
    with pytest.raises(F.CaseError, match="stops the tender"):
        F.approve("3")
    assert F.vendors_for(F.build_case("2"))["ran"] is False and not vendors
    c = F.approve("2")
    assert c.stage == "vendors" and c.approved_at


def test_vendor_scoring_on_approved_codes_with_preticks(vendors):
    v = F.vendors_for(F.approve("2"))
    assert vendors == [("Waterproofing", ["W.P-COBA"])]
    g = v["groups"][0]
    assert [x["picked"] for x in g["vendors"]] == [True, True, False] and g["confirmed"] is False
    c = F.pick_vendors("2", "Waterproofing", ["v1", "v3"])
    assert c.stage == "rfq" and c.vendor_picks == {"Waterproofing": ["v1", "v3"]}
    assert [x["picked"] for x in F.vendors_for(c)["groups"][0]["vendors"]] == [True, False, True]
    with pytest.raises(F.CaseError):
        F.pick_vendors("2", "Waterproofing", ["nobody"])
    with pytest.raises(F.CaseError):
        F.pick_vendors("2", "Cement", ["v1"])


def test_changing_an_item_withdraws_approval_and_picks(vendors):
    F.approve("2")
    F.pick_vendors("2", "Waterproofing", ["v1"])
    c = F.decide("2", "2-r3", "use_code", ["W.P-COBA"])
    assert c.stage == "approve" and c.approved_at is None and c.vendor_picks == {}
    F.approve("2")
    assert F.build_case("2").vendor_picks == {}


def test_rfq_draft_per_picked_vendor(vendors):
    from procurement.cases import rfq as Q

    F.approve("2")
    c = F.pick_vendors("2", "Waterproofing", ["v1", "v3"])
    ds = Q.drafts(c)
    assert [(d.vendor_id, d.to) for d in ds] == [("v1", "v1@vendor.example"), ("v3", "")]
    b = ds[0].body
    assert "W.P-COBA: WATERPROOFING WITH BRICK BAT COBA, 300 SQM" in b
    assert "the attached quantity sheet" in b
    assert all(t in b for t in Q.STANDARD_TERMS)
    assert ds[0].subject.startswith("Seeking quotation for waterproofing at Riverside")


def test_rfq_send_is_test_inbox_only_and_completes_the_case(vendors, monkeypatch):
    from procurement.cases import rfq as Q

    F.approve("2")
    F.pick_vendors("2", "Waterproofing", ["v1", "v2"])
    with pytest.raises(R.SendError) as e:
        Q.send("2", "v1", "v1@vendor.example", "s", "b")
    assert e.value.status == 403
    with pytest.raises(R.SendError):
        Q.send("2", "v3", "x@test.example", "s", "b", mark_only=True)  # v3 isn't picked
    Q.send("2", "v1", "v1@vendor.example", "s", "b", mark_only=True)
    c = F.build_case("2")
    assert c.stage == "rfq" and [r.vendor_id for r in c.rfqs] == ["v1"]
    Q.send("2", "v2", "v2@vendor.example", "s", "b", mark_only=True)
    assert F.build_case("2").stage == "rfq_sent"


def test_api_round_trip(vendors):
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    items = cl.get("/api/cases").json()["items"]
    assert {i["uid"]: i["stage"] for i in items} == {"3": "item_codes", "2": "approve", "1": "item_codes"}
    s = cl.get("/api/cases/summary").json()
    assert s["ready"] and s["stats"][1]["value"] == 1
    c = cl.get("/api/cases/1").json()
    assert c["draft"]["needed"] and c["send"] == {"smtp": False, "allowed_to": []}
    assert "requirement" in c and "body" in c["email"] and c["rfq_drafts"] == []
    r = cl.post("/api/cases/1/lines/1-r4", json={"action": "use_code", "codes": ["WP.01", "WP.02"]})
    assert r.status_code == 200 and r.json()["counts"]["decided"] == 1
    assert cl.post("/api/cases/1/lines/1-r4", json={"action": "use_code", "codes": ["ZZ"]}).status_code == 400
    assert cl.post("/api/cases/1/reply", json={"to": "site@example.com", "subject": "s", "body": "b"}).status_code == 403
    assert cl.post("/api/cases/1/reply", json={"to": "site@example.com", "subject": "s", "body": "b", "mark_only": True}).json()["stage"] == "waiting_on_site"
    assert cl.post("/api/cases/1/approve", json={}).status_code == 400
    assert cl.post("/api/cases/3/checks/sheet", json={"resolved": True}).json()["stage"] == "approve"
    assert cl.get("/api/cases/2/vendors").json()["ran"] is False
    assert cl.post("/api/cases/2/approve", json={}).json()["stage"] == "vendors"
    assert cl.get("/api/cases/2/vendors").json()["groups"][0]["category"] == "Waterproofing"
    c = cl.post("/api/cases/2/vendors", json={"category": "Waterproofing", "vendor_ids": ["v1"]}).json()
    assert c["stage"] == "rfq" and [d["vendor_id"] for d in c["rfq_drafts"]] == ["v1"]
    c = cl.post("/api/cases/2/rfq/v1", json={"to": "v1@vendor.example", "subject": "s", "body": "b", "mark_only": True}).json()
    assert c["stage"] == "rfq_sent" and c["rfq_drafts"][0]["sent"]["via"] == "marked"
    assert cl.get("/api/cases/99").status_code == 404


def test_a_warning_alone_does_not_need_a_reply():
    c = F.build_case("2")
    c.email_checks = [WARN | {"resolved": False}]
    assert R.draft(c).needed is False
    c.email_checks = [BLOCK | {"resolved": False}]
    assert R.draft(c).needed is True


# ── Review fixes ──


def test_rfqs_sent_before_a_change_do_not_count_after_reapproval(vendors):
    from procurement.cases import rfq as Q

    F.approve("2")
    F.pick_vendors("2", "Waterproofing", ["v1"])
    Q.send("2", "v1", "v1@vendor.example", "s", "b", mark_only=True)
    assert F.build_case("2").stage == "rfq_sent"
    F.decide("2", "2-r3", "use_code", ["W.P-COBA"])  # items changed: approval, picks and sent RFQs withdrawn
    F.approve("2")
    c = F.pick_vendors("2", "Waterproofing", ["v1"])
    assert c.stage == "rfq" and c.rfqs == [] and Q.drafts(c)[0].sent is None


def test_approval_lapses_if_the_approved_codes_change(vendors, monkeypatch):
    F.approve("2")
    state = F.load_state("2")
    state["approved_codes"] = {"2-r3": ["WP.01"]}  # as if the ERP list changed the match since approval
    F.save_state("2", state)
    assert F.build_case("2").stage == "approve"


def test_picks_must_be_on_the_shortlist_and_not_empty(vendors):
    F.approve("2")
    with pytest.raises(F.CaseError, match="Pick at least one"):
        F.pick_vendors("2", "Waterproofing", [])
    with pytest.raises(F.CaseError, match="Not on the Waterproofing shortlist"):
        F.pick_vendors("2", "Waterproofing", ["v9"])


def test_reply_does_not_promise_vendors_before_questions_are_answered():
    body = R.draft(F.build_case("1")).body
    assert "go to vendors now" not in body and "once the points above are confirmed" in body


def test_mailbox_emails_never_share_a_demo_id(tmp_path, monkeypatch):
    store = tmp_path / "mail"
    store.mkdir()
    (store / "1.json").write_text(json.dumps(RECORDS[1] | {"status": "read", "uid": "1"}))
    monkeypatch.setattr(mailbox, "STORE", store)
    uids = [r["uid"] for r in F.records()]
    assert "m1" in uids and "1" in uids
    assert F.build_case("m1").subject == "Roof coba, Tower-2"


def test_subject_with_a_line_break_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("REPLY_ALLOWED_TO", "@test.example")
    monkeypatch.setenv("GMAIL_ADDRESS", "d@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    with pytest.raises(R.SendError, match="line breaks"):
        R.send("1", "qa@test.example", "a\nb", "body")
