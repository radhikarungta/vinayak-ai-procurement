"""One case per requirement email: requirement lines -> item codes -> vendors.

Every line is matched to an ERP item code (procurement/itemcodes). A line goes to vendors only when
its code is settled:

- ready:     an exact match with no conflict on the code.
- to reply:  a conflict, a partial match or no match. It is held and listed in the reply to the
             site, until a person picks a code (or skips the line).

Problems with the email itself (the intake checks: a missing quantity, a vendor named without a
reason, …) are listed in the reply too.

Then a person approves the items to go with (settled lines; skipped lines stay out). Only after
approval does the vendor step (procurement/shortlist) run, per category on the approved item codes.
The person picks the vendors, and an RFQ is drafted per picked vendor (procurement/cases/rfq.py).
The quotes that come back are compared, a vendor is chosen and the HOD is asked to approve it
(procurement/cases/quotes.py). Changing an item after approval withdraws the approval, the vendor
picks and everything after them.

People decide; decisions are saved in data/cases/<uid>.json (git-ignored, CASES_DIR overrides).
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .. import mailbox
from ..itemcodes import master as M
from . import db as D
from ..itemcodes.match import LineMatch, lines_from_record, match_lines

DEMO_INBOX = M.ROOT / "samples" / "demo_inbox.json"

IssueKind = Literal["conflict", "partial", "no_match"]
LineStatus = Literal["ready", "to_reply", "decided", "skipped"]
Stage = Literal["item_codes", "waiting_on_site", "approve", "vendors", "rfq", "rfq_sent", "quotes", "hod", "done"]

# Which vendor category a line belongs to, from words in the line or the code's group.
CATEGORY_WORDS: list[tuple[set[str], str]] = [
    ({"WATERPROOFING", "GROUTING", "COBA", "SEALANT", "MEMBRANE", "SCREED", "PUNNING"}, "Waterproofing"),
    ({"CEMENT", "OPC", "PPC"}, "Cement"),
    ({"TMT", "REBAR", "STEEL"}, "Steel"),
    ({"SCAFFOLDING", "SCAFFOLD"}, "Scaffolding"),
    ({"ADHESIVE", "TILE", "TILES"}, "Tiles & adhesive"),
    ({"CONCRETE", "RMC"}, "Concrete"),
]
CODE_GROUPS = {"WP": "Waterproofing", "CEM": "Cement", "TMT": "Steel", "STL": "Steel", "SCF": "Scaffolding",
               "ADH": "Tiles & adhesive", "TIL": "Tiles & adhesive"}


class CaseError(RuntimeError):
    pass


class Issue(BaseModel):
    kind: IssueKind
    text: str


class Decision(BaseModel):
    action: Literal["use_code", "skip", "ask_site"]
    codes: list[str] = []
    note: str | None = None
    at: str


class CaseLine(BaseModel):
    id: str
    text: str
    quantity: float | None
    uom: str | None
    source: str | None
    match: LineMatch
    issues: list[Issue]
    status: LineStatus
    codes: list[str]  # the settled item codes (empty unless ready or decided)
    category: str | None
    decision: Decision | None = None


class SentReply(BaseModel):
    to: str
    subject: str
    body: str
    at: str
    via: Literal["smtp", "marked"]
    asked: list[str] | None = None  # line ids and "check:<id>" the reply covered (None: sent before this was kept)
    message_id: str | None = None  # set on a sent email, so the answer can be linked back (site_reply.match_case)


class SentRfq(SentReply):
    vendor_id: str
    vendor: str
    category: str
    attached: list[str] = []


class Case(BaseModel):
    uid: str
    subject: str | None
    sender: str | None
    date: str | None
    project: str | None
    lines: list[CaseLine]
    email_checks: list[dict]  # intake checks of severity block/warn
    stage: Stage
    counts: dict[str, int]
    replies: list[SentReply]
    approved_at: str | None
    vendor_picks: dict[str, list[str]]  # category -> vendor ids
    rfqs: list[SentRfq]
    quotes_in: int = 0  # quotes in for the picked vendors (uploaded or from the tender folder)
    award: dict[str, dict] = {}  # category -> the vendor chosen, with the reason
    hod: dict | None = None  # the email to the HOD: to, subject, at, via, approved_by_hod_at
    site_replies: list[dict] = []  # the site's answers (site_reply.SiteReply), oldest first
    site_replied: bool = False  # the site answered after our last reply and lines are still open: review it
    headline: str


# ── Where decisions live ───────────────────────────────────────────────────────────────────────


def cases_dir() -> Path:
    return Path(os.environ.get("CASES_DIR") or (M.ROOT / "data" / "cases"))


def _store_path(uid: str) -> Path:
    if not uid.isalnum():
        raise CaseError("Bad case id.")
    return cases_dir() / f"{uid}.json"


def load_state(uid: str) -> dict:
    p = _store_path(uid)  # also checks the id
    if D.enabled():
        return D.load(uid) or {"decisions": {}, "replies": []}
    return json.loads(p.read_text()) if p.exists() else {"decisions": {}, "replies": []}


def _withdraw(state: dict) -> None:
    """Items changed: the approval and the vendor picks made on them no longer stand."""
    state.pop("approved_at", None)
    state.pop("approved_codes", None)
    state.pop("vendor_picks", None)
    state.pop("rfq_items", None)
    state.setdefault("rfq_history", []).extend(state.pop("rfqs", []))  # sent under an earlier approval
    for k in ("quote_uploads", "pack_links", "manual_rates", "sim_quotes", "compare", "award"):  # the quotes answered those RFQs
        if state.get(k):
            state.setdefault("quotes_history", []).append({k: state[k], "at": now()})
        state.pop(k, None)
    if state.get("hod"):
        state.setdefault("hod_history", []).append(state["hod"])
    state.pop("hod", None)


def all_states() -> list[tuple[str, dict]]:
    """(uid, saved state) for every case with decisions saved."""
    d = cases_dir()
    return [(p.stem, json.loads(p.read_text())) for p in sorted(d.glob("*.json"))] if d.exists() else []


# ── The activity log: every change, who made it and when ───────────────────────────────────────

# Changes are made by request handlers and by the agent's background thread: each change (load, modify,
# save) runs under this lock (cases/api.py _do, and each agent move), and files are replaced atomically
# so a poll never reads half a file.
STATE_LOCK = threading.RLock()

Actor = Literal["person", "agent", "site", "vendor", "system"]
_ACTOR: ContextVar[str] = ContextVar("actor", default="person")


@contextmanager
def acting_as(actor: str):
    """Changes made inside are logged as made by `actor` (the agent runs its steps inside this)."""
    tok = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(tok)


def log_event(state: dict, kind: str, text: str, *, actor: str | None = None, **detail) -> None:
    """Add one line to the case's activity log (saved with the state)."""
    ev = {"at": now(), "actor": actor or _ACTOR.get(), "kind": kind, "text": text}
    if detail:
        ev["detail"] = detail
    state.setdefault("events", []).append(ev)


def save_state(uid: str, state: dict) -> None:
    p = _store_path(uid)
    if D.enabled():  # Supabase (procurement/cases/db.py); local files otherwise
        return D.save(uid, state)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Which requirement emails there are ─────────────────────────────────────────────────────────


def records() -> list[dict]:
    """Demo inbox plus emails read from the mailbox, newest first. Each has uid and result."""
    out: dict[str, dict] = {}
    p = Path(DEMO_INBOX)
    if p.exists():
        for r in json.loads(p.read_text()):
            out[str(r["uid"])] = r
    from . import arrivals  # noqa: PLC0415 - demo emails that "landed" later (arrivals imports this module)

    for r in arrivals.records():
        out[str(r["uid"])] = r
    for uid in mailbox.saved_uids():
        r = mailbox.load_result(uid)
        if r and r.get("status") == "read" and r.get("result"):
            out[f"m{uid}"] = r | {"uid": f"m{uid}"}  # never collides with a demo email's id or saved state
    return sorted(out.values(), key=lambda r: int(str(r["uid"]).lstrip("m") or 0) if str(r["uid"]).lstrip("m").isdigit() else 0, reverse=True)


def record(uid: str) -> dict:
    r = next((r for r in records() if str(r["uid"]) == uid), None)
    if r is None:
        raise CaseError(f"No requirement email with id {uid}.")
    return r


# ── Building a case ────────────────────────────────────────────────────────────────────────────


def category_of(text: str, codes: list[str]) -> str | None:
    toks = M.tokens(text)
    for words, cat in CATEGORY_WORDS:
        if toks & words:
            return cat
    for c in codes:
        grp = c.split(".")[0].split("-")[0].upper()
        if grp in CODE_GROUPS:
            return CODE_GROUPS[grp]
    return None


def issues_of(m: LineMatch) -> list[Issue]:
    out = [Issue(kind="conflict", text=f.text) for f in m.flags if f.tone == "bad"]
    if m.kind == "partial":
        out.append(Issue(kind="partial", text=m.reason))
    elif m.kind == "none":
        out.append(Issue(kind="no_match", text=m.reason))
    return out


def build_line(m: LineMatch, decision: Decision | None) -> CaseLine:
    issues = issues_of(m)
    auto = [m.code, *m.also] if m.code else []
    if decision and decision.action == "use_code":
        status, codes = "decided", decision.codes
    elif decision and decision.action == "skip":
        status, codes = "skipped", []
    elif decision and decision.action == "ask_site":
        status, codes = "to_reply", []
    elif not issues:
        status, codes = "ready", auto
    else:
        status, codes = "to_reply", []
    ln = m.line
    return CaseLine(id=ln.id, text=ln.text, quantity=ln.quantity, uom=ln.uom, source=ln.source, match=m,
                    issues=issues, status=status, codes=codes, category=category_of(ln.text, codes or auto),
                    decision=decision)


def build_case(uid: str, *, master: M.Master | None = None, use_llm: bool | None = False, rec: dict | None = None) -> Case:
    rec = rec or record(uid)
    m = master or M.get_master()
    res = rec.get("result") or {}
    email = res.get("email") or {}
    state = load_state(uid)
    decisions = {k: Decision(**v) for k, v in state.get("decisions", {}).items()}
    matched = match_lines(lines_from_record(rec, m), m, use_llm=use_llm).lines
    lines = [build_line(x, decisions.get(x.line.id)) for x in matched]
    resolved = set(state.get("checks_resolved", []))
    checks = [c | {"resolved": c.get("id") in resolved} for c in res.get("checks", []) if c.get("severity") in ("block", "warn")]
    open_checks = [c for c in checks if not c["resolved"]]
    blocking = [c for c in open_checks if c["severity"] == "block"]

    counts = {s: sum(ln.status == s for ln in lines) for s in ("ready", "to_reply", "decided", "skipped")}
    open_lines = [ln for ln in lines if ln.status == "to_reply"]
    counts |= {k: sum(any(i.kind == k for i in ln.issues) for ln in open_lines) for k in ("conflict", "partial", "no_match")}
    counts["email_checks"] = len(open_checks)
    counts["blocking"] = len(blocking)
    settled = counts["ready"] + counts["decided"]
    picks = state.get("vendor_picks") or {}
    rfqs = [SentRfq(**r) for r in state.get("rfqs", [])]
    approved = state.get("approved_at") if not (counts["to_reply"] or blocking) and settled else None
    if approved and state.get("approved_codes") != approved_codes(lines):
        approved = None  # the items changed since approval (e.g. the ERP list was edited): approve again
    picked = {v for ids in picks.values() for v in ids}
    quotes_in, awards, hod = 0, state.get("award") or {}, state.get("hod") or {}
    if approved and picked:
        from . import quotes as CQ  # noqa: PLC0415 - quotes imports this module

        quotes_in = len(CQ.sources(uid, picks, lines, state)[0])
    awarded = bool(approved and picked) and all(c in awards for c, ids in picks.items() if ids)
    last = (state.get("replies") or [None])[-1]
    new_qs = bool(last) and asked_since(last, open_lines, blocking, state.get("redecided", []))
    site = state.get("site_replies") or []
    replied = bool(site) and (not last or site[-1]["at"] >= last["at"]) and bool(counts["to_reply"] or blocking)
    if counts["to_reply"] or blocking:
        stage: Stage = "waiting_on_site" if last and not new_qs and not replied else "item_codes"
    elif not settled:
        stage = "item_codes"
    elif not approved:
        stage = "approve"
    elif not picked:
        stage = "vendors"
    elif {(c, v) for c, ids in picks.items() for v in ids} - {(r.category, r.vendor_id) for r in rfqs}:
        stage = "rfq"
    elif awarded:
        stage = "done" if hod.get("approved_by_hod_at") else "hod"
    elif quotes_in:
        stage = "quotes"
    else:
        stage = "rfq_sent"

    n = len(lines)
    if stage == "approve":
        skipped = f", {counts['skipped']} skipped" if counts["skipped"] else ""
        headline = f"{settled} of {n} line{'s' * (n != 1)} have an item code{skipped}. Approve the items to go to vendors."
    elif stage == "vendors":
        headline = "Items approved. Pick the vendors for the RFQ."
    elif stage == "rfq":
        left = len({(c, v) for c, ids in picks.items() for v in ids} - {(r.category, r.vendor_id) for r in rfqs})
        headline = f"{len(picked)} vendor{'s' * (len(picked) != 1)} picked. {left} RFQ{'s' * (left != 1)} still to send."
    elif stage == "rfq_sent":
        headline = f"RFQs sent to all {len(picked)} picked vendor{'s' * (len(picked) != 1)}. Waiting for quotes: upload them in step 5."
    elif stage == "quotes":
        headline = f"{quotes_in} quote{'s' * (quotes_in != 1)} in from {len(picked)} picked vendor{'s' * (len(picked) != 1)}. Compare them and choose the vendor."
    elif stage in ("hod", "done"):
        who = ", ".join(a["vendor"] for c, a in awards.items() if picks.get(c))
        headline = (f"HOD approved {who}." if stage == "done" else
                    f"{who} chosen. Sent to the HOD, awaiting approval." if hod else f"{who} chosen. Next: email the HOD for approval.")
    else:
        bits = [f"{counts[k]} {w}" for k, w in (("conflict", "with a code conflict"), ("partial", "partly matched"), ("no_match", "with no item code")) if counts[k]]
        bits += [f"{len(blocking)} email problem{'s' * (len(blocking) != 1)} that stop{'s' * (len(blocking) == 1)} the tender"] if blocking else []
        headline = f"{settled} of {n} line{'s' * (n != 1)} ready. To ask the site: " + (", ".join(bits) or "nothing") + "."
        if replied:
            headline += " The site replied: review its answers below."
        elif stage == "waiting_on_site":
            headline += " Reply sent; waiting on the site."
        elif new_qs:
            headline += " New questions since the last reply: send another."
    return Case(uid=uid, subject=email.get("subject"), sender=email.get("sender"), date=email.get("date"),
                project=((res.get("requirement") or {}).get("project") or {}).get("value"),
                lines=lines, email_checks=checks, stage=stage, counts=counts,
                replies=[SentReply(**r) for r in state.get("replies", [])], approved_at=approved,
                vendor_picks=picks if approved else {}, rfqs=rfqs, quotes_in=quotes_in,
                award={c: a for c, a in awards.items() if picks.get(c)} if approved else {}, hod=hod or None,
                site_replies=site, site_replied=replied, headline=headline)


def asked_ids(case: Case) -> list[str]:
    """What a reply sent now covers: the undecided lines and the open email checks."""
    return [ln.id for ln in case.lines if ln.status == "to_reply"] + [f"check:{c['id']}" for c in case.email_checks if not c.get("resolved")]


def asked_since(last: dict, open_lines: list[CaseLine], blocking: list[dict], redecided: list[str] = ()) -> bool:
    """A question the last reply to the site didn't ask: a line or a blocking check it didn't cover,
    or a line marked "ask the site" after it went that it didn't ask (or that was decided since)."""
    asked = last.get("asked")
    if asked is not None and ({ln.id for ln in open_lines} | {f"check:{c['id']}" for c in blocking}) - set(asked):
        return True
    sent = datetime.fromisoformat(last["at"])
    return any(ln.decision and ln.decision.action == "ask_site"
               and (ln.id in redecided or (asked is None and datetime.fromisoformat(ln.decision.at) > sent)) for ln in open_lines)


def decide(uid: str, line_id: str, action: str, codes: list[str] | None = None, note: str | None = None,
           *, master: M.Master | None = None) -> Case:
    m = master or M.get_master()
    case = build_case(uid, master=m)
    line = next((ln for ln in case.lines if ln.id == line_id), None)
    if line is None:
        raise CaseError(f"No line {line_id} in this requirement.")
    state = load_state(uid)
    if action != "ask_site" and state.get("replies") and line_id not in state.setdefault("redecided", []):
        state["redecided"].append(line_id)  # settled after the reply went: asking about it again is a new question
    if action == "clear":
        state["decisions"].pop(line_id, None)
    else:
        codes = [c.strip() for c in (codes or []) if c.strip()]
        if action == "use_code":
            if not codes:
                raise CaseError("Pick at least one item code.")
            known = m.codes()
            offered = {c.code for c in line.match.candidates} | set(line.match.also)
            if line.match.suggestion:
                offered.add(line.match.suggestion.code)
            unknown = [c for c in codes if c not in known and c not in offered]
            if unknown:
                raise CaseError(f"Not an ERP code or a suggested new code: {', '.join(unknown)}.")
        state["decisions"][line_id] = Decision(action=action, codes=codes if action == "use_code" else [], note=note, at=now()).model_dump()
    what = {"use_code": f"Item code {' + '.join(codes or [])} for", "skip": "Left out", "ask_site": "To ask the site about",
            "clear": "Undid the decision on"}[action]
    log_event(state, "item_code", f"{what} \"{line.text}\"" + (f" ({note})" if note else ""), line=line_id)
    if state.get("approved_at"):
        log_event(state, "withdrawn", "Items changed: the approval and everything after it were withdrawn.", actor="system")
    _withdraw(state)
    save_state(uid, state)
    return build_case(uid, master=m)


def resolve_check(uid: str, check_id: str, resolved: bool, *, master: M.Master | None = None) -> Case:
    """Mark an email check as sorted out (e.g. the site resent the quantity sheet), or reopen it."""
    case = build_case(uid, master=master)
    if check_id not in {c.get("id") for c in case.email_checks}:
        raise CaseError(f"No open check {check_id} on this requirement.")
    state = load_state(uid)
    done = set(state.get("checks_resolved", []))
    done = done | {check_id} if resolved else done - {check_id}
    state["checks_resolved"] = sorted(done)
    title = next(c["title"] for c in case.email_checks if c.get("id") == check_id)
    log_event(state, "check", f"{'Sorted' if resolved else 'Reopened'}: {title}", check=check_id)
    if not resolved:
        _withdraw(state)
    save_state(uid, state)
    return build_case(uid, master=master)


# ── Approval, vendors, picks ───────────────────────────────────────────────────────────────────


def approve(uid: str, approved: bool = True, *, master: M.Master | None = None) -> Case:
    case = build_case(uid, master=master)
    state = load_state(uid)
    if approved:
        if case.counts["to_reply"]:
            raise CaseError(f"{case.counts['to_reply']} line(s) still need an item code: pick one, skip the line, or wait for the site.")
        if case.counts["blocking"]:
            raise CaseError("A problem with the email stops the tender. Sort it out first.")
        if not case.counts["ready"] + case.counts["decided"]:
            raise CaseError("No line has an item code to approve.")
        codes = approved_codes(case.lines)
        if state.get("approved_at") and state.get("approved_codes") != codes:
            _withdraw(state)  # the items changed since the last approval: its picks, quotes and HOD email don't stand
        state["approved_at"] = now()
        state["approved_codes"] = codes
        n = len({c for v in codes.values() for c in v})
        log_event(state, "approve", f"Approved the items to go to vendors ({n} item code{'s' * (n != 1)}).")
    else:
        log_event(state, "approve", "Withdrew the approval.")
        _withdraw(state)
    save_state(uid, state)
    return build_case(uid, master=master)


def approved_codes(lines: list[CaseLine]) -> dict[str, list[str]]:
    return {ln.id: ln.codes for ln in lines if ln.status in ("ready", "decided")}


def approved_groups(case: Case) -> tuple[dict[str, list[CaseLine]], list[str]]:
    """Approved lines by vendor category, and the lines with no known category."""
    groups: dict[str, list[CaseLine]] = {}
    no_cat = []
    for ln in case.lines:
        if ln.status not in ("ready", "decided"):
            continue
        if ln.category:
            groups.setdefault(ln.category, []).append(ln)
        else:
            no_cat.append(ln.text)
    return groups, no_cat


def vendors_for(case: Case) -> dict:
    """Vendor scoring per category on the approved item codes, with the person's picks."""
    from ..shortlist import score as S

    if not case.approved_at:
        return {"ran": False, "reason": "Approve the items first. Vendors are scored on the approved item codes.", "groups": [], "notes": []}
    groups, no_cat = approved_groups(case)
    out = []
    for cat, lines in groups.items():
        codes = category_codes(lines)
        res = S.shortlist(cat, codes)
        picked = case.vendor_picks.get(cat)
        vendors = [v.model_dump() | {"picked": (v.vendor_id in picked) if picked is not None else v.selected} for v in res.vendors]
        out.append({"category": cat, "item_codes": codes, "selected": res.selected, "confirmed": picked is not None,
                    "vendors": vendors, "excluded": [e.model_dump() for e in res.excluded], "notes": res.notes, "min_rfq": S.MIN_RFQ})
    notes = [f"No vendor category known for: {t}. It can't go to vendors yet." for t in no_cat]
    return {"ran": True, "groups": out, "notes": notes}


def category_codes(lines: list[CaseLine]) -> list[str]:
    return list(dict.fromkeys(c for ln in lines for c in ln.codes))


def rfq_key(vendor_id: str, category: str) -> str:
    return f"{vendor_id}|{category}"


def pick_rfq_items(uid: str, vendor_id: str, category: str, codes: list[str], *, master: M.Master | None = None) -> Case:
    """Which of the category's approved item codes go on the RFQ to one vendor (none saved: all)."""
    case = build_case(uid, master=master)
    if not case.approved_at:
        raise CaseError("Approve the items first.")
    groups, _ = approved_groups(case)
    if category not in groups:
        raise CaseError(f"No approved items in {category}.")
    if vendor_id not in case.vendor_picks.get(category, []):
        raise CaseError(f"Vendor {vendor_id} isn't picked for {category}.")
    if any((r.vendor_id, r.category) == (vendor_id, category) for r in case.rfqs):
        raise CaseError("That RFQ was already sent; its items can't change.")
    codes = [c.strip() for c in codes if c.strip()]
    if not codes:
        raise CaseError("An RFQ needs at least one item.")
    avail = category_codes(groups[category])
    bad = [c for c in codes if c not in avail]
    if bad:
        raise CaseError(f"Not an approved {category} item: {', '.join(bad)}.")
    state = load_state(uid)
    state.setdefault("rfq_items", {})[rfq_key(vendor_id, category)] = [c for c in avail if c in codes]
    save_state(uid, state)
    return build_case(uid, master=master)


def pick_vendors(uid: str, category: str, vendor_ids: list[str], *, master: M.Master | None = None) -> Case:
    case = build_case(uid, master=master)
    if not case.approved_at:
        raise CaseError("Approve the items first.")
    groups, _ = approved_groups(case)
    if category not in groups:
        raise CaseError(f"No approved items in {category}.")
    from ..shortlist import score as S

    if not vendor_ids:
        raise CaseError("Pick at least one vendor.")
    codes = category_codes(groups[category])
    allowed = {v.vendor_id for v in S.shortlist(category, codes).vendors}
    bad = [v for v in vendor_ids if v not in allowed]
    if bad:
        raise CaseError(f"Not on the {category} shortlist (unknown, blocked or another category): {', '.join(bad)}.")
    state = load_state(uid)
    new = list(dict.fromkeys(vendor_ids))
    if new != (state.get("vendor_picks") or {}).get(category):
        from . import quotes as CQ  # noqa: PLC0415

        (state.get("compare") or {}).pop(category, None)
        CQ.drop_award(state, category)
    state.setdefault("vendor_picks", {})[category] = new
    from ..vendors import store as V  # noqa: PLC0415

    names = {v.id: v.name for v in V.load_all()}
    log_event(state, "vendors", f"Picked {len(new)} vendor{'s' * (len(new) != 1)} for {category}: {', '.join(names.get(v, v) for v in new)}.",
              category=category)
    save_state(uid, state)
    return build_case(uid, master=master)
