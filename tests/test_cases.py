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
    assert "attached" not in b and ds[0].attachment is None  # demo email: the sheet file wasn't kept
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


def test_reapproving_after_the_codes_changed_drops_the_old_picks_award_and_hod(vendors):
    F.approve("2")
    F.pick_vendors("2", "Waterproofing", ["v1"])
    state = F.load_state("2")
    state["award"] = {"Waterproofing": {"vendor_id": "v1", "vendor": "Vendor 1", "reason": "r", "at": "x"}}
    state["hod"] = {"to": "hod@test.example", "approved_by_hod_at": "x"}
    state["approved_codes"] = {"2-r3": ["WP.01"]}  # the ERP list changed the match since approval
    F.save_state("2", state)
    c = F.approve("2")
    st = F.load_state("2")
    assert c.stage == "vendors" and c.vendor_picks == {} and not c.award and c.hod is None
    assert not any(k in st for k in ("vendor_picks", "award", "hod")) and st["hod_history"][0]["approved_by_hod_at"] == "x"
    F.pick_vendors("2", "Waterproofing", ["v1"])
    assert F.approve("2").vendor_picks == {"Waterproofing": ["v1"]}  # same codes: approving again keeps the picks


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


# ── RFQ items per vendor, list resilience, new questions after a reply, the sheet attachment ──


def settle_and_approve_1():
    F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"])
    F.decide("1", "1-r5", "skip")
    F.decide("1", "1-r6", "use_code", ["WP.01"])
    F.approve("1")
    return F.pick_vendors("1", "Waterproofing", ["v1", "v2"])


def test_rfq_items_default_to_all_and_can_be_narrowed_per_vendor(vendors):
    from procurement.cases import rfq as Q

    ds = Q.drafts(settle_and_approve_1())
    assert [i.code for i in ds[0].available] == ["WP.01", "WP.02"] and ds[0].selected == ["WP.01", "WP.02"]
    assert ds[0].available[1].text == "WATERPROOFING AT TOILET WALL (UPTO 450 MM HIGHT)"
    assert "80 SQM in total across WP.01 and WP.02, split to be confirmed" in ds[0].body
    c = F.pick_rfq_items("1", "v1", "Waterproofing", ["WP.02"])
    v1, v2 = Q.drafts(c)
    assert v1.selected == ["WP.02"] and v2.selected == ["WP.01", "WP.02"]
    assert F.load_state("1")["rfq_items"] == {"v1|Waterproofing": ["WP.02"]}
    assert "1. WP.02: WATERPROOFING AT TOILET WALL" in v1.body and "WP.01" not in v1.body
    assert "split to be confirmed" not in v1.body and "quantity to be confirmed (part of the site's 80 SQM)" in v1.body
    v1 = Q.drafts(F.pick_rfq_items("1", "v1", "Waterproofing", ["WP.01"]))[0]
    assert v1.body.count("WP.01:") == 3 and "WP.02" not in v1.body


def test_rfq_items_are_checked(vendors):
    from procurement.cases import rfq as Q

    settle_and_approve_1()
    with pytest.raises(F.CaseError, match="at least one item"):
        F.pick_rfq_items("1", "v1", "Waterproofing", [])
    with pytest.raises(F.CaseError, match="Not an approved Waterproofing item"):
        F.pick_rfq_items("1", "v1", "Waterproofing", ["WP.01", "W.P-COBA"])
    with pytest.raises(F.CaseError, match="isn't picked"):
        F.pick_rfq_items("1", "v3", "Waterproofing", ["WP.01"])
    with pytest.raises(F.CaseError, match="No approved items"):
        F.pick_rfq_items("1", "v1", "Cement", ["WP.01"])
    Q.send("1", "v1", "v1@vendor.example", "s", "b", category="Waterproofing", mark_only=True)
    with pytest.raises(F.CaseError, match="already sent"):
        F.pick_rfq_items("1", "v1", "Waterproofing", ["WP.01"])
    F.pick_rfq_items("1", "v2", "Waterproofing", ["WP.01"])
    F.decide("1", "1-r5", "clear")  # items changed: the per-vendor selection goes with the approval
    assert "rfq_items" not in F.load_state("1")


def test_rfq_items_endpoint(vendors):
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    settle_and_approve_1()
    url = "/api/cases/1/rfq/v1/items?category=Waterproofing"
    r = cl.post(url, json={"codes": ["WP.01"]})
    assert r.status_code == 200
    d = r.json()["rfq_drafts"][0]
    assert d["selected"] == ["WP.01"] and [i["code"] for i in d["available"]] == ["WP.01", "WP.02"]
    assert cl.post(url, json={"codes": []}).status_code == 400
    assert cl.post(url, json={"codes": ["ZZ"]}).status_code == 400
    assert cl.post("/api/cases/1/rfq/v3/items?category=Waterproofing", json={"codes": ["WP.01"]}).status_code == 400
    assert cl.post("/api/cases/1/rfq/v1/items", json={"codes": ["WP.01"]}).status_code == 422  # category is required


def test_list_shows_a_broken_case_as_an_error_and_reads_each_record_once(monkeypatch):
    F.save_state("2", {"decisions": {"2-r3": {"action": "bogus", "at": "x"}}, "replies": []})
    reads = []
    real = F.record
    monkeypatch.setattr(F, "record", lambda uid: reads.append(uid) or real(uid))
    items = cases_api.list_cases()["items"]
    assert [(i["uid"], i["stage"]) for i in items] == [("3", "item_codes"), ("2", "error"), ("1", "item_codes")] and reads == []
    assert items[1]["subject"] == "Roof coba, Tower-2" and "couldn't be opened" in items[1]["headline"]
    s = cases_api.summary()
    assert s["headline"].startswith("3 requirement emails") and "1 couldn't be opened" in s["headline"]


def test_a_question_added_after_the_reply_needs_a_new_reply():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    assert F.build_case("1").stage == "waiting_on_site"
    c = F.decide("1", "1-r3", "ask_site", note="Which floor?")  # a ready line, asked about after the reply went
    assert c.stage == "item_codes" and "New questions since the last reply" in c.headline
    R.send("1", "site@example.com", "Re: x", "body 2", mark_only=True)
    assert F.build_case("1").stage == "waiting_on_site"


def test_undoing_a_code_after_the_reply_needs_a_new_reply():
    F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"])
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    assert F.build_case("1").stage == "waiting_on_site"
    assert F.decide("1", "1-r4", "clear").stage == "item_codes"


def test_blocking_check_asked_in_the_reply_waits_and_old_replies_still_count():
    R.send("3", "site@example.com", "Re: x", "body", mark_only=True)
    assert F.load_state("3")["replies"][0]["asked"] == ["check:sheet"]
    assert F.build_case("3").stage == "waiting_on_site"
    # A reply saved before "asked" was kept: only an ask-the-site mark made after it reopens the case.
    F.save_state("1", {"decisions": {}, "replies": [{"to": "s@example.com", "subject": "s", "body": "b", "at": "2026-11-20T10:00:00+00:00", "via": "marked"}]})
    assert F.build_case("1").stage == "waiting_on_site"
    assert F.decide("1", "1-r3", "ask_site").stage == "item_codes"


@pytest.fixture
def mailbox_case(tmp_path):
    """A requirement read from the mailbox (uid m7) whose quantity sheet file was kept at intake."""
    store = tmp_path / "mail"
    store.mkdir()
    r = json.loads(json.dumps(RECORDS[1]))
    r["result"]["quantities"]["filename"] = "Roof_quantities.xlsx"
    (store / "7.json").write_text(json.dumps(r | {"status": "read", "uid": "7"}))
    p = mailbox.attachment_path("7", "Roof_quantities.xlsx")
    p.parent.mkdir()
    p.write_bytes(b"PK sheet bytes")
    return "m7"


def test_rfq_attaches_the_quantity_sheet_when_it_was_kept(vendors, mailbox_case, monkeypatch):
    from procurement.cases import rfq as Q

    F.approve(mailbox_case)
    d = Q.drafts(F.pick_vendors(mailbox_case, "Waterproofing", ["v1"]))[0]
    assert d.attachment == "Roof_quantities.xlsx" and "the attached quantity sheet" in d.body
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setenv("REPLY_ALLOWED_TO", "@test.example")
    monkeypatch.setenv("GMAIL_ADDRESS", "desk@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    r = Q.send(mailbox_case, "v1", "qa@test.example", "s", d.body, smtp_factory=FakeSMTP)
    att = list(sent[0].iter_attachments())
    assert [a.get_filename() for a in att] == ["Roof_quantities.xlsx"] and att[0].get_content() == b"PK sheet bytes"
    assert r.attached == ["Roof_quantities.xlsx"]
    assert Q.send(mailbox_case, "v1", "qa@test.example", "s", d.body, mark_only=True).attached == []
    mailbox.attachment_path("7", "Roof_quantities.xlsx").unlink()  # file gone: no promise of an attachment
    assert "attached" not in Q.drafts(F.build_case(mailbox_case))[0].body


def test_rfq_for_part_of_the_sheet_does_not_attach_it(vendors, tmp_path, monkeypatch):
    from procurement.cases import rfq as Q

    store = tmp_path / "mail"
    store.mkdir()
    r = rec("8", "Floor and coba", [sheet_line(3, "Waterproofing at toilet floor", 120), sheet_line(4, "Waterproofing with brick bat coba", 300)])
    r["result"]["quantities"]["filename"] = "Both.xlsx"
    (store / "8.json").write_text(json.dumps(r | {"status": "read"}))
    p = mailbox.attachment_path("8", "Both.xlsx")
    p.parent.mkdir()
    p.write_bytes(b"PK")
    F.approve("m8")
    F.pick_vendors("m8", "Waterproofing", ["v1"])
    assert Q.drafts(F.build_case("m8"))[0].attachment == "Both.xlsx"
    d = Q.drafts(F.pick_rfq_items("m8", "v1", "Waterproofing", ["WP.01"]))[0]
    assert d.attachment is None and "attached" not in d.body and "the items and points below" in d.body
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setenv("REPLY_ALLOWED_TO", "@test.example")
    monkeypatch.setenv("GMAIL_ADDRESS", "desk@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    assert Q.send("m8", "v1", "qa@test.example", "s", d.body, smtp_factory=FakeSMTP).attached == []
    assert list(sent[0].iter_attachments()) == []


def test_asking_the_site_about_a_line_the_reply_already_asked_does_not_reopen():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)  # asked about 1-r4 among others
    assert "1-r4" in F.load_state("1")["replies"][0]["asked"]
    c = F.decide("1", "1-r4", "ask_site", note="Floor or wall?")
    assert c.stage == "waiting_on_site" and "New questions" not in c.headline


def test_a_line_decided_after_the_reply_then_asked_again_needs_a_new_reply():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    assert "1-r4" in F.load_state("1")["replies"][0]["asked"]
    F.decide("1", "1-r4", "use_code", ["WP.01"])
    c = F.decide("1", "1-r4", "ask_site", note="Which floor now?")  # same second as the reply: ordering comes from state
    assert c.stage == "item_codes" and "New questions since the last reply" in c.headline
    R.send("1", "site@example.com", "Re: x", "body 2", mark_only=True)
    assert F.build_case("1").stage == "waiting_on_site"
