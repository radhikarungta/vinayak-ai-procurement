"""The requirement agent: from the email to the HOD draft on its own, stopping where a person decides.
Plus the activity log, the email thread and simulated vendor quotes. Made-up data, no network."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement.cases import agent as A
from procurement.cases import api as cases_api
from procurement.cases import flow as F
from procurement.cases import quotes as CQ
from procurement.cases import timeline as T
from procurement.shortlist import score as S
from tests.test_cases import setup  # noqa: F401 - autouse: made-up inbox (cases 1, 2, 3), master, cases dir


@pytest.fixture
def three(monkeypatch):
    """Three made-up vendors, all on the shortlist (SOP 05 wants three)."""
    from procurement.vendors import store as V

    vs = [V.Vendor(id=f"v{i}", name=f"Vendor {i}", categories=["Waterproofing"], email=f"v{i}@vendor.example",
                   created_at="2026-11-21", updated_at="2026-11-21") for i in (1, 2, 3)]
    monkeypatch.setattr(V, "load_all", lambda: vs)
    state = {"pick": 3}

    def fake(category, codes):
        scored = [S.Scored(vendor_id=v.id, name=v.name, status=v.status, categories=v.categories, score=90 - 10 * i,
                           coverage=0.5, rank=i + 1, selected=i < state["pick"], factors=[]) for i, v in enumerate(vs)]
        return S.Shortlist(category=category, item_codes=codes, medians={}, weights={}, vendors=scored, excluded=[],
                           selected=state["pick"], notes=[])

    monkeypatch.setattr(S, "shortlist", fake)
    return state


def test_clear_requirement_runs_to_the_hod_draft(three):
    r = A.run("2")
    assert r.mode == "demo" and r.stopped == "ready_for_hod" and r.step == "s7"
    assert [s.stage for s in r.steps] == ["approve", "vendors", "rfq", "rfq_sent", "quotes"]
    c = F.build_case("2")
    assert c.stage == "hod" and not c.hod  # drafted, never sent: the HOD's approval is the human sign-off
    aw = c.award["Waterproofing"]
    assert aw["override"] is False and "Lowest" in aw["reason"]
    assert CQ.view(c)["hod_draft"]["body"]


def test_events_say_who_did_what(three):
    F.approve("2")  # a person's click
    A.run("2")
    ev = F.load_state("2")["events"]
    assert ev[0]["actor"] == "person" and ev[0]["kind"] == "approve"
    assert {e["actor"] for e in ev[1:]} == {"agent", "vendor"}  # the vendors' quotes are theirs
    assert any(e["actor"] == "agent" and e["kind"] == "award" for e in ev)
    assert ev[-1]["kind"] == "agent" and "Agent finished" in ev[-1]["text"]


def test_site_questions_answered_then_on_to_the_hod(three):
    r = A.run("1")
    texts = [s.text for s in r.steps]
    assert texts[0].startswith("Asked the site 3 questions") and "simulated" in texts[1]
    assert texts[2].startswith("Accepted the site's answers: 4 questions")
    assert r.stopped == "ready_for_hod", r.reason
    c = F.build_case("1")
    assert c.counts["to_reply"] == 0 and set(c.award) == {ln.category for ln in c.lines if ln.status in ("ready", "decided")}
    kinds = [m["label"] for m in T.thread(c)]
    assert kinds[0] == "Requirement email" and "Our reply to the site" in kinds and "The site's answer" in kinds
    assert sum(k.startswith("RFQ to") for k in kinds) >= 3 and sum(k.startswith("Quote from") for k in kinds) >= 3
    actors = {e["actor"] for e in T.history(c)}
    assert {"site", "agent", "vendor"} <= actors


def test_too_few_vendors_stops_for_a_person(three):
    three["pick"] = 2
    r = A.run("2")
    assert r.stopped == "needs_you" and r.step == "s4" and "SOP 05 asks for 3" in r.reason
    assert F.build_case("2").stage == "vendors"


def test_close_call_stops_for_a_person(three, monkeypatch):
    monkeypatch.setattr(A, "CLOSE_CALL_PCT", 100.0)
    r = A.run("2")
    assert r.stopped == "needs_you" and r.step == "s6" and "close call" in r.reason
    assert not F.build_case("2").award


def test_live_requirement_stops_before_any_email(three, monkeypatch):
    monkeypatch.setattr(A, "is_demo", lambda uid: False)
    r = A.run("2")
    assert r.mode == "live" and r.stopped == "needs_you" and r.step == "s4" and "doesn't send real emails" in r.reason
    assert not F.build_case("2").rfqs
    monkeypatch.setattr(A, "is_demo", lambda uid: False)
    r = A.run("1")
    assert r.stopped == "needs_you" and r.step == "s2" and not F.build_case("1").replies


def test_blocking_email_problem_is_asked_and_settled(three):
    r = A.run("3")  # the quantity sheet is missing: the site resends it
    assert r.stopped == "ready_for_hod", r.reason
    assert F.build_case("3").email_checks[0]["resolved"]


def test_runs_kept_and_rerun_continues(three):
    three["pick"] = 2
    assert A.run("2").stopped == "needs_you"
    three["pick"] = 3
    assert A.run("2").stopped == "ready_for_hod"
    runs = F.load_state("2")["agent_runs"]
    assert [x["stopped"] for x in runs] == ["needs_you", "ready_for_hod"]


def test_simulated_quote_rules(three):
    with pytest.raises(F.CaseError, match="demo"):
        CQ.simulate_quote("m4", "v1", "Waterproofing")
    F.approve("2")
    F.pick_vendors("2", "Waterproofing", ["v1", "v2", "v3"])
    with pytest.raises(F.CaseError, match="RFQ"):
        CQ.simulate_quote("2", "v1", "Waterproofing")


def test_rate_recommendation_without_quantities():
    from procurement.quotes.models import Comparison, LineCompare, TenderLine, VendorCompare

    def vc(name, rate):
        return VendorCompare(vendor=name, file="f", read_confidence="high", needs_vision=False, quantity_basis="unknown", as_quoted_total=None,
                             repriced_total=None, complete=True, lines=[LineCompare(sl_no=1, code="C", vendor_unit="bag", vendor_qty=None, rate=rate,
                                                                                     rate_per_tender_unit=rate, repriced=None, as_quoted=None, comparable=False)])

    cmp = Comparison(tender=[TenderLine(sl_no=1, code="C", description="cement", unit="bag", qty=None)], comparable_codes=[],
                     vendors=[vc("A", 400), vc("B", 390), vc("C", 410)], non_comparable=[], insights=[], notes=[])
    rec = CQ.recommend(cmp, ["a", "b", "c"])
    assert rec["vendor_id"] == "b" and rec["by_rate"] and rec["margin_pct"] == pytest.approx(2.56, abs=0.01)
    assert "₹390.00/bag" in rec["reason"]


def test_api_agent_thread_history(three):
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    c = cl.get("/api/cases/2").json()
    assert c["agent"] is None and c["thread"][0]["label"] == "Requirement email" and c["history"][0]["kind"] == "email_in"
    cl.post("/api/cases/2/agent")
    wait()
    c = cl.get("/api/cases/2").json()
    assert c["agent"]["stopped"] == "ready_for_hod" and not c["agent"]["running"] and c["stage"] == "hod"
    assert set(cl.post("/api/cases/agent/run-all").json()["started"]) == {"1", "2", "3"}
    wait()
    items = {i["uid"]: i for i in cl.get("/api/cases").json()["items"]}
    assert items["2"]["agent"]["stopped"] == "ready_for_hod" and items["1"]["agent"]["last"]


def wait(timeout=10.0):
    import time

    end = time.time() + timeout
    while A.RUNNING and time.time() < end:
        time.sleep(0.02)
    assert not A.RUNNING


def test_live_progress_is_saved_and_a_cut_off_run_says_so(three, monkeypatch):
    seen = []
    real = A._live
    monkeypatch.setattr(A, "_live", lambda uid, r: (seen.append(len(r.steps)), real(uid, r)))
    A.run("2")
    assert seen[:3] == [0, 1, 2] and "agent_live" not in F.load_state("2")
    st = F.load_state("2")
    st["agent_live"] = {"at": "x", "mode": "demo", "steps": [], "running": True, "stopped": None, "reason": None, "step": None}
    F.save_state("2", st)
    assert T.last_run(F.build_case("2"))["stopped"] == "error"  # nothing running in this process: cut off


def test_new_email_lands_and_the_agent_takes_it(three):
    from procurement.cases import arrivals as AR

    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    got = cl.post("/api/cases/arrive").json()
    assert got["uid"] == "4"  # next after the demo inbox's 1, 2, 3
    wait()
    c = F.build_case("4")
    assert c.stage == "hod" and T.last_run(c)["stopped"] == "ready_for_hod"
    assert T.history(c)[0]["kind"] == "email_in" and T.history(c)[0]["actor"] == "system"
    assert "4" in {str(r["uid"]) for r in F.records()}
    assert AR.arrive()["uid"] == "5"  # another one: the next template, the next id


def test_new_email_manual_mode_leaves_it_to_a_person(three):
    app = FastAPI()
    app.include_router(cases_api.router)
    got = TestClient(app).post("/api/cases/arrive?mode=manual").json()
    assert got["mode"] == "manual"
    wait()
    c = F.build_case(got["uid"])
    assert T.last_run(c) is None and c.stage != "hod"  # the agent never started


def test_new_email_checkpoints_stop_for_a_person_then_carry_on(three):
    from procurement.cases import arrivals as AR

    uid = AR.arrive("checkpoints")["uid"]
    r = A.run(uid)
    assert r.stopped == "needs_you" and r.step == "s3" and "approve" in r.reason
    F.approve(uid, True)  # the person approves
    r = A.run(uid)
    assert r.stopped == "needs_you" and r.step == "s6" and "recommended" in r.reason
    c = F.build_case(uid)
    for g in CQ.view(c)["categories"]:  # the person confirms the recommended vendor
        CQ.award(uid, g["category"], g["recommended"]["vendor_id"])
    r = A.run(uid)
    assert r.stopped == "ready_for_hod"


# ── review fixes ───────────────────────────────────────────────────────────────────────────────


def test_changing_items_drops_simulated_quotes(three):
    A.run("2")
    assert F.load_state("2")["sim_quotes"]
    F.approve("2", False)
    st = F.load_state("2")
    assert "sim_quotes" not in st and any("sim_quotes" in h for h in st["quotes_history"])


def test_start_shows_running_at_once(three, monkeypatch):
    import threading

    gate = threading.Event()
    real = A.run
    monkeypatch.setattr(A, "run", lambda uid, **kw: (gate.wait(5), real(uid, **kw))[1])
    assert A.start("2")
    assert T.last_run(F.build_case("2"))["running"] is True  # before the thread has done anything
    assert A.start("2") is False  # one run per case
    gate.set()
    wait()
    assert T.last_run(F.build_case("2"))["stopped"] == "ready_for_hod"


def test_unexpected_error_is_recorded_not_left_running(three, monkeypatch):
    monkeypatch.setattr(A, "_approve", lambda case: {}["boom"])
    r = A.run("2")
    assert r.stopped == "error" and "KeyError" in r.reason and "2" not in A.RUNNING
    st = F.load_state("2")
    assert "agent_live" not in st and st["agent_runs"][-1]["stopped"] == "error"


def test_a_suggestion_that_doesnt_fit_its_question_is_left_for_a_person(three, monkeypatch):
    from procurement.cases import site_reply as SR

    real = SR.simulated_answer

    def odd(case):
        body, sugg = real(case)
        for s in sugg:
            if not s.ref.startswith("check:"):
                s.action, s.codes = "resolved", []  # "sorted" on a line: not something decide() takes
        return body, sugg

    monkeypatch.setattr(SR, "simulated_answer", odd)
    r = A.run("1")
    assert r.stopped == "needs_you" and r.step == "s2" and "still need" in r.reason


def test_inline_mode_steps_on_each_check(three, monkeypatch):
    """Serverless: no thread; each check (tick) takes one step until the run stops."""
    monkeypatch.setenv("AGENT_INLINE", "1")
    assert A.start("2") is True and A.start("2") is False  # one run at a time
    assert T.last_run(F.build_case("2"))["running"]
    for _ in range(30):
        A.tick("2")
        if not T.last_run(F.build_case("2"))["running"]:
            break
    r = T.last_run(F.build_case("2"))
    assert r["stopped"] == "ready_for_hod" and len(r["steps"]) == 5 and not A.RUNNING
