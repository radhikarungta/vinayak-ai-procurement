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
Changing an item after approval withdraws the approval and the vendor picks.

People decide; decisions are saved in data/cases/<uid>.json (git-ignored, CASES_DIR overrides).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .. import mailbox
from ..itemcodes import master as M
from ..itemcodes.match import LineMatch, lines_from_record, match_lines

DEMO_INBOX = M.ROOT / "samples" / "demo_inbox.json"

IssueKind = Literal["conflict", "partial", "no_match"]
LineStatus = Literal["ready", "to_reply", "decided", "skipped"]
Stage = Literal["item_codes", "waiting_on_site", "approve", "vendors", "rfq", "rfq_sent"]

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


class SentRfq(SentReply):
    vendor_id: str
    vendor: str
    category: str


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
    headline: str


# ── Where decisions live ───────────────────────────────────────────────────────────────────────


def cases_dir() -> Path:
    return Path(os.environ.get("CASES_DIR") or (M.ROOT / "data" / "cases"))


def _store_path(uid: str) -> Path:
    if not uid.isalnum():
        raise CaseError("Bad case id.")
    return cases_dir() / f"{uid}.json"


def load_state(uid: str) -> dict:
    p = _store_path(uid)
    return json.loads(p.read_text()) if p.exists() else {"decisions": {}, "replies": []}


def _withdraw(state: dict) -> None:
    """Items changed: the approval and the vendor picks made on them no longer stand."""
    state.pop("approved_at", None)
    state.pop("approved_codes", None)
    state.pop("vendor_picks", None)
    state.setdefault("rfq_history", []).extend(state.pop("rfqs", []))  # sent under an earlier approval


def save_state(uid: str, state: dict) -> None:
    p = _store_path(uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


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


def build_case(uid: str, *, master: M.Master | None = None, use_llm: bool | None = False) -> Case:
    rec = record(uid)
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
    if counts["to_reply"] or blocking:
        stage: Stage = "waiting_on_site" if state.get("replies") else "item_codes"
    elif not settled:
        stage = "item_codes"
    elif not approved:
        stage = "approve"
    elif not picked:
        stage = "vendors"
    elif {(c, v) for c, ids in picks.items() for v in ids} - {(r.category, r.vendor_id) for r in rfqs}:
        stage = "rfq"
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
        headline = f"RFQs sent to all {len(picked)} picked vendor{'s' * (len(picked) != 1)}. Next: read the quotes."
    else:
        bits = [f"{counts[k]} {w}" for k, w in (("conflict", "with a code conflict"), ("partial", "partly matched"), ("no_match", "with no item code")) if counts[k]]
        bits += [f"{len(blocking)} email problem{'s' * (len(blocking) != 1)} that stop{'s' * (len(blocking) == 1)} the tender"] if blocking else []
        headline = f"{settled} of {n} line{'s' * (n != 1)} ready. To ask the site: " + (", ".join(bits) or "nothing") + "."
        if stage == "waiting_on_site":
            headline += " Reply sent; waiting on the site."
    return Case(uid=uid, subject=email.get("subject"), sender=email.get("sender"), date=email.get("date"),
                project=((res.get("requirement") or {}).get("project") or {}).get("value"),
                lines=lines, email_checks=checks, stage=stage, counts=counts,
                replies=[SentReply(**r) for r in state.get("replies", [])], approved_at=approved,
                vendor_picks=picks if approved else {}, rfqs=rfqs, headline=headline)


def decide(uid: str, line_id: str, action: str, codes: list[str] | None = None, note: str | None = None,
           *, master: M.Master | None = None) -> Case:
    m = master or M.get_master()
    case = build_case(uid, master=m)
    line = next((ln for ln in case.lines if ln.id == line_id), None)
    if line is None:
        raise CaseError(f"No line {line_id} in this requirement.")
    state = load_state(uid)
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
        state["approved_at"] = now()
        state["approved_codes"] = approved_codes(case.lines)
    else:
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
        codes = list(dict.fromkeys(c for ln in lines for c in ln.codes))
        res = S.shortlist(cat, codes)
        picked = case.vendor_picks.get(cat)
        vendors = [v.model_dump() | {"picked": (v.vendor_id in picked) if picked is not None else v.selected} for v in res.vendors]
        out.append({"category": cat, "item_codes": codes, "selected": res.selected, "confirmed": picked is not None,
                    "vendors": vendors, "excluded": [e.model_dump() for e in res.excluded], "notes": res.notes, "min_rfq": S.MIN_RFQ})
    notes = [f"No vendor category known for: {t}. It can't go to vendors yet." for t in no_cat]
    return {"ran": True, "groups": out, "notes": notes}


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
    codes = list(dict.fromkeys(c for ln in groups[category] for c in ln.codes))
    allowed = {v.vendor_id for v in S.shortlist(category, codes).vendors}
    bad = [v for v in vendor_ids if v not in allowed]
    if bad:
        raise CaseError(f"Not on the {category} shortlist (unknown, blocked or another category): {', '.join(bad)}.")
    state = load_state(uid)
    state.setdefault("vendor_picks", {})[category] = list(dict.fromkeys(vendor_ids))
    save_state(uid, state)
    return build_case(uid, master=master)
