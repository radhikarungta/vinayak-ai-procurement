"""The requirement agent: takes a requirement email as far as it can on its own, up to the email to the
HOD, and stops only where a person has to decide.

It uses the same functions a person's clicks do (flow.decide, flow.approve, reply.send, rfq.send,
quotes.award, …), inside `flow.acting_as("agent")`, so every change it makes is in the case's activity
log as made by the agent, with its reason. Nothing here bypasses a check a person would hit.

What it decides on its own (when everything is clear):
- accepts the site's answers that were read clearly (each rests on a verbatim quote from the reply);
- approves the items once every line has a settled item code and no email problem stops the tender;
- picks the shortlist's top vendors (SOP 05: three per category);
- awards the lowest complete like-for-like quote.

Where it stops ("needs you"): a line or check the site's answer doesn't settle, a line with no vendor
category, fewer vendors than the SOP asks for, no like-for-like cheapest quote or two quotes within 1%.
It never sends the HOD email: it drafts it and stops, the HOD's approval being the human sign-off.

Demo emails (from samples/demo_inbox.json) run end to end: the reply to the site and the RFQs are
marked as sent (no real inbox), the site's answer and the vendors' quotes are simulated. A requirement
from the real mailbox stops before any email would go out (the drafts are ready for a person).

Each run is kept in the state (agent_runs, newest last).
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel

from .. import llm
from ..itemcodes import master as M
from . import flow as F
from . import quotes as CQ
from . import reply as R
from . import rfq as Q
from . import site_reply as SR

StopKind = Literal["needs_you", "ready_for_hod", "done", "error"]
CLOSE_CALL_PCT = 1.0  # quotes this close: a person chooses
RUNNING: set[str] = set()  # cases with a run in progress in this process
_LOCK = threading.Lock()


def step_delay() -> float:
    """Seconds between the agent's steps, so a person can watch it work (AGENT_STEP_DELAY, default 3; 0 in tests)."""
    try:
        return max(0.0, float(os.environ.get("AGENT_STEP_DELAY", "3")))
    except ValueError:
        return 3.0


class Step(BaseModel):
    at: str
    stage: str
    text: str


class Run(BaseModel):
    at: str
    finished_at: str | None = None
    mode: Literal["demo", "live"]
    steps: list[Step] = []
    stopped: StopKind | None = None
    reason: str | None = None  # why it stopped, for the person
    step: str | None = None  # the step tab the person should open (s2 … s7)
    running: bool = False  # still working (state["agent_live"]); the page polls until it isn't


class _Stop(Exception):
    def __init__(self, kind: StopKind, reason: str, step: str | None = None):
        super().__init__(reason)
        self.kind, self.reason, self.step = kind, reason, step


def is_demo(uid: str) -> bool:
    return uid.isdigit()


def _need(reason: str, step: str) -> _Stop:
    return _Stop("needs_you", reason, step)


# ── One move per stage ─────────────────────────────────────────────────────────────────────────


def _item_codes(case: F.Case, demo: bool, run: Run, client: Any) -> str:
    open_n = case.counts["to_reply"] + case.counts["blocking"]
    if not open_n:
        if not case.lines:
            raise _need("No requirement lines were read from the email.", "s2")
        raise _need("Some lines have no settled item code.", "s2")
    if case.site_replied:
        latest = case.site_replies[-1]
        if latest.get("read") != "read":
            raise _need(f"The site's answer isn't read yet ({latest.get('error') or latest.get('read')}).", "s2")
        open_lines = {ln.id for ln in case.lines if ln.status == "to_reply"}
        open_checks = {c["id"] for c in case.email_checks if not c.get("resolved")}
        done = 0
        for s in latest.get("suggestions", []):
            if s["action"] == "unclear":
                continue
            # Only an action that fits the question: "sorted" for an email check, a code or "leave out" for a line.
            if s["ref"].startswith("check:"):
                if s["action"] == "resolved" and s["ref"][6:] in open_checks:
                    F.resolve_check(case.uid, s["ref"][6:], True)
                    done += 1
            elif s["ref"] in open_lines and s["action"] in ("use_code", "skip"):
                F.decide(case.uid, s["ref"], s["action"], s["codes"], note=s.get("note") or None)
                done += 1
        left = F.build_case(case.uid)
        rest = left.counts["to_reply"] + left.counts["blocking"]
        if rest:
            raise _need(f"The site's answer settled {done} question{'s' * (done != 1)}; {rest} still need{'s' * (rest == 1)} you.", "s2")
        return f"Accepted the site's answers: {done} question{'s' * (done != 1)} settled, each on the site's own words."
    if case.stage == "waiting_on_site":
        if not demo:
            raise _need("Waiting for the site's answer to our reply.", "s2")
        SR.simulate(case.uid, client=client)
        return "The site answered our reply (simulated for the demo)."
    d = R.draft(case)
    if not demo:
        raise _need(f"{open_n} question{'s' * (open_n != 1)} for the site: the reply is drafted in step 2. The agent doesn't send real emails yet.", "s2")
    R.send(case.uid, d.to or case.sender or "site@demo.invalid", d.subject, d.body, mark_only=True, case=case)
    return f"Asked the site {open_n} question{'s' * (open_n != 1)} (reply marked as sent: demo email)."


def _approve(case: F.Case) -> str:
    F.approve(case.uid, True)
    n = case.counts["ready"] + case.counts["decided"]
    skipped = f", {case.counts['skipped']} left out" if case.counts["skipped"] else ""
    return f"Approved the items: {n} line{'s' * (n != 1)} with a settled item code{skipped}, no email problem stopping the tender."


def _vendors(case: F.Case) -> str:
    from ..shortlist import score as S

    vf = F.vendors_for(case)
    if vf["notes"]:
        raise _need(" ".join(vf["notes"]), "s4")
    if not vf["groups"]:
        raise _need("No approved line has a vendor category.", "s4")
    out = []
    for g in vf["groups"]:
        if g["confirmed"]:
            continue
        top = [v["vendor_id"] for v in g["vendors"] if v["picked"]]
        if not top:
            raise _need(f"No vendor on the {g['category']} shortlist.", "s4")
        if len(top) < S.MIN_RFQ:
            raise _need(f"Only {len(top)} vendor{'s' * (len(top) != 1)} on the {g['category']} shortlist; SOP 05 asks for {S.MIN_RFQ}. "
                        "Pick in step 4 (or add vendors).", "s4")
        F.pick_vendors(case.uid, g["category"], top)
        names = [v["name"] for v in g["vendors"] if v["vendor_id"] in top]
        out.append(f"{g['category']}: {', '.join(names)}")
    return "Picked the top-scored vendors (SOP 05): " + "; ".join(out) + "."


def _rfqs(case: F.Case, demo: bool) -> str:
    todo = [d for d in Q.drafts(case) if not d.sent]
    if not demo:
        raise _need(f"{len(todo)} RFQ{'s' * (len(todo) != 1)} drafted in step 4. The agent doesn't send real emails yet.", "s4")
    for d in todo:
        # A demo vendor with no email on file: a readable placeholder (".invalid" can never deliver).
        to = d.to or f"{re.sub(r'[^a-z0-9]+', '-', d.vendor.lower()).strip('-')[:40] or d.vendor_id}@no-email-on-file.invalid"
        Q.send(case.uid, d.vendor_id, to, d.subject, d.body, category=d.category, mark_only=True)
    return f"Sent {len(todo)} RFQ{'s' * (len(todo) != 1)} with the six standard terms (marked as sent: demo email)."


def _quotes_in(case: F.Case, demo: bool) -> str:
    view = CQ.view(case)
    waiting = [(g["category"], w) for g in view["categories"] for w in g["waiting"]]
    if not waiting:
        raise _need("No quote to compare yet.", "s6")
    if not demo:
        raise _need(f"Waiting for {len(waiting)} quote{'s' * (len(waiting) != 1)}: upload them in step 5 as they come in.", "s6")
    for cat, w in waiting:
        CQ.simulate_quote(case.uid, w["vendor_id"], cat)
    return f"{len(waiting)} vendor{'s' * (len(waiting) != 1)} quoted (simulated for the demo)."


def _award(case: F.Case, demo: bool) -> str:
    view = CQ.view(case)
    out = []
    for g in view["categories"]:
        if g["award"]:
            continue
        if g["waiting"] and not demo:
            raise _need(f"{len(g['waiting'])} {g['category']} quote(s) still to come: wait, or choose on the quotes in (step 5).", "s6")
        if g["waiting"]:
            return _quotes_in(case, demo)
        rec = g["recommended"]
        if rec is None:
            raise _need(f"No complete quote to recommend for {g['category']}: check the quotes in step 5.", "s6")
        if not rec["like_for_like"]:
            raise _need(f"{g['category']}: the quotes aren't on the same quantities, so the cheapest isn't clear. Choose in step 5.", "s6")
        if rec.get("margin_pct") is not None and rec["margin_pct"] < CLOSE_CALL_PCT:
            raise _need(f"{g['category']}: {rec['vendor']} is cheapest by only {rec['margin_pct']:.1f}%, a close call. "
                        "Weigh terms and past performance and choose in step 5.", "s6")
        CQ.award(case.uid, g["category"], rec["vendor_id"])
        out.append(f"{g['category']}: {rec['vendor']} ({rec['reason']})")
    return "Chose the vendor: " + "; ".join(out)


def _move(case: F.Case, demo: bool, run: Run, client: Any) -> str:
    st = case.stage
    if st in ("item_codes", "waiting_on_site"):
        return _item_codes(case, demo, run, client)
    if st == "approve":
        return _approve(case)
    if st == "vendors":
        return _vendors(case)
    if st == "rfq":
        return _rfqs(case, demo)
    if st == "rfq_sent":
        return _quotes_in(case, demo)
    if st == "quotes":
        return _award(case, demo)
    if st == "hod":
        if case.hod:
            raise _Stop("ready_for_hod", "The email to the HOD has gone; waiting for the HOD's approval.", "s7")
        raise _Stop("ready_for_hod", "Everything is sorted. The email to the HOD is drafted: review it and send it in step 6.", "s7")
    if st == "done":
        raise _Stop("done", "The HOD approved. Nothing left to do.", "s7")
    raise _need(f"Unknown stage {st}.", "s2")


# ── The run ────────────────────────────────────────────────────────────────────────────────────


def _live(uid: str, r: Run) -> None:
    state = F.load_state(uid)
    state["agent_live"] = r.model_dump()
    F.save_state(uid, state)


def start(uid: str, *, master: M.Master | None = None) -> bool:
    """Run the agent in the background (one run per case at a time). False if one is already running."""
    F.build_case(uid, master=master)  # a bad id fails now, in the request
    with _LOCK:
        if uid in RUNNING:
            return False
        RUNNING.add(uid)
    _queued([uid])
    threading.Thread(target=_bg, args=([uid],), daemon=True, name=f"agent-{uid}").start()
    return True


def start_many(uids: list[str]) -> list[str]:
    """One after another in one background thread (the overview shows them move in turn)."""
    with _LOCK:
        todo = [u for u in uids if u not in RUNNING]
        RUNNING.update(todo)
    _queued(todo)
    if todo:
        threading.Thread(target=_bg, args=(todo,), daemon=True, name="agent-all").start()
    return todo


def _queued(uids: list[str]) -> None:
    """Show 'Agent working' at once (before the thread gets to it), so the page starts following it."""
    for uid in uids:
        with F.STATE_LOCK:
            _live(uid, Run(at=F.now(), mode="demo" if is_demo(uid) else "live", running=True))


def _bg(uids: list[str]) -> None:
    for uid in uids:
        try:
            run(uid, _claimed=True)
        except Exception:  # noqa: BLE001 - logged; the next case still runs
            import logging

            logging.getLogger(__name__).exception("Agent run on %s failed", uid)
        finally:
            with _LOCK:
                RUNNING.discard(uid)


def run(uid: str, *, client: Any = None, max_moves: int = 25, master: M.Master | None = None, delay: float | None = None,
        _claimed: bool = False) -> Run:
    """Move the requirement on until a person is needed (or the HOD email is ready). Returns the run.
    Waits `delay` seconds (default step_delay()) before each step and saves progress, so the page can show it live."""
    F.build_case(uid, master=master)  # a bad id fails here, before anything is logged
    demo = is_demo(uid)
    delay = step_delay() if delay is None else delay
    r = Run(at=F.now(), mode="demo" if demo else "live", running=True)
    if not _claimed:
        with _LOCK:
            RUNNING.add(uid)
    try:
        return _run(uid, r, demo, delay, client, max_moves, master)
    finally:
        if not _claimed:
            with _LOCK:
                RUNNING.discard(uid)


def _run(uid: str, r: Run, demo: bool, delay: float, client: Any, max_moves: int, master: M.Master | None) -> Run:
    with F.acting_as("agent"), F.STATE_LOCK:
        state = F.load_state(uid)
        F.log_event(state, "agent", f"Agent started ({'demo: sends, site answers and quotes simulated' if demo else 'live: stops before any email goes out'}).")
        F.save_state(uid, state)
        _live(uid, r)
    last = None
    try:
        for _ in range(max_moves):
            if delay:
                time.sleep(delay)  # outside the lock: a person's clicks go through between steps
            with F.acting_as("agent"), F.STATE_LOCK:
                case = F.build_case(uid, master=master)
                key = (case.stage, tuple(sorted(case.counts.items())), len(case.replies), len(case.site_replies), len(case.rfqs), case.quotes_in)
                if key == last:
                    raise _need(f"The agent made no progress at stage {case.stage}.", "s2")
                last = key
                text = _move(case, demo, r, client)
                r.steps.append(Step(at=F.now(), stage=case.stage, text=text))
                _live(uid, r)
        else:
            raise _need("The agent stopped after too many steps.", None)
    except _Stop as s:
        r.stopped, r.reason, r.step = s.kind, s.reason, s.step
    except (F.CaseError, R.SendError, llm.LLMError) as e:
        r.stopped, r.reason = "error", str(e)
    except Exception as e:  # noqa: BLE001 - recorded on the run, never left half-done
        import logging

        logging.getLogger(__name__).exception("Agent run on %s failed", uid)
        r.stopped, r.reason = "error", f"Something went wrong ({type(e).__name__}): {e}"
    with F.acting_as("agent"), F.STATE_LOCK:
        r.finished_at, r.running = F.now(), False
        state = F.load_state(uid)
        state.pop("agent_live", None)
        label = {"needs_you": "Agent stopped: needs you", "ready_for_hod": "Agent finished", "done": "Agent finished", "error": "Agent stopped on an error"}
        F.log_event(state, "agent", f"{label[r.stopped]}. {r.reason}", steps=len(r.steps))
        state["agent_runs"] = (state.get("agent_runs") or [])[-19:] + [r.model_dump()]
        F.save_state(uid, state)
    return r
