"""Two read-only views of one requirement, built from what is saved:

- thread: every email on it, in time order. The requirement email, our replies to the site and the
  site's answers, the RFQs to vendors and the quotes back, the email to the HOD. Withdrawn RFQs and
  earlier HOD emails are included, marked as superseded.
- history: the activity log (flow.log_event): every change, who made it (a person, the agent, the
  site, a vendor, the system) and when, opening with the email landing.
"""

from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime

from . import flow as F


def _iso(date: str | None) -> str | None:
    if not date:
        return None
    try:
        d = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return None
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d.tzinfo else d.isoformat(timespec="seconds")


def _mail(direction: str, label: str, who: str, at: str | None, subject: str | None, body: str, *, to: str | None = None,
          via: str | None = None, party: str, note: str | None = None, superseded: bool = False) -> dict:
    return {"direction": direction, "label": label, "from": who, "to": to, "at": at, "subject": subject, "body": body,
            "via": via, "party": party, "note": note, "superseded": superseded}


def thread(case: F.Case) -> list[dict]:
    rec = F.record(case.uid)
    email = (rec.get("result") or {}).get("email") or {}
    state = F.load_state(case.uid)
    out = [_mail("in", "Requirement email", case.sender or "the site", _iso(case.date), case.subject, email.get("body", ""),
                 party="site", note=", ".join(a.get("filename", "") for a in email.get("attachments", [])) or None)]
    for r in state.get("replies", []):
        out.append(_mail("out", "Our reply to the site", "Procurement", r["at"], r["subject"], r["body"], to=r["to"], via=r["via"], party="site"))
    for s in state.get("site_replies", []):
        out.append(_mail("in", "The site's answer", s.get("sender") or "the site", s["at"], s.get("subject"), s["body"], via=s["via"], party="site"))
    for r, old in [(r, False) for r in state.get("rfqs", [])] + [(r, True) for r in state.get("rfq_history", [])]:
        out.append(_mail("out", f"RFQ to {r['vendor']} ({r['category']})", "Procurement", r["at"], r["subject"], r["body"], to=r["to"], via=r["via"],
                         party="vendor", note=", ".join(r.get("attached") or []) or None, superseded=old))
    for k, sim in (state.get("sim_quotes") or {}).items():
        q = sim["quote"]
        rows = [f"{ln['code'] or ln['sl_no']}: {ln['description']} · ₹{ln['rate']:,.2f}/{ln['unit']}" + (f" × {ln['qty']:,.2f}" if ln.get("qty") else "")
                for ln in q["lines"]]
        terms = [t["text"] for t in q.get("terms", [])]
        out.append(_mail("in", f"Quote from {q['vendor']}", q["vendor"], sim["at"], f"Quotation: {k.split('|', 1)[1]}", "\n".join(rows + [""] + terms),
                         via="simulated", party="vendor", note=q["file"]))
    from ..vendors import store as V  # noqa: PLC0415

    names = {v.id: v.name for v in V.load_all()} if state.get("quote_uploads") else {}
    for k, up in (state.get("quote_uploads") or {}).items():
        who = names.get(k.split("|")[0], k.split("|")[0])
        out.append(_mail("in", f"Quote from {who}", who, up.get("at"), "Quotation (uploaded PDF)", "",
                         via="upload", party="vendor", note=up.get("original")))
    for h, old in [(h, True) for h in state.get("hod_history", [])] + ([(state["hod"], False)] if state.get("hod") else []):
        out.append(_mail("out", "Email to the HOD", "Procurement", h["at"], h["subject"], h["body"], to=h["to"], via=h["via"], party="hod",
                         note=f"Approved by the HOD {h['approved_by_hod_at'][:16].replace('T', ' ')}" if h.get("approved_by_hod_at") else None,
                         superseded=old))
    return sorted(out, key=lambda m: m["at"] or "")


def history(case: F.Case) -> list[dict]:
    state = F.load_state(case.uid)
    events = list(state.get("events", []))
    if any(e.get("kind") == "email_in" for e in events):
        return events
    first = {"at": _iso(case.date), "actor": "site", "kind": "email_in", "text": f"Requirement email landed: {case.subject or '(no subject)'}."}
    return [first] + events


def last_run(case: F.Case) -> dict | None:
    """The run in progress (running: True), else the last finished one. A run cut off by a restart says so."""
    from . import agent as A  # noqa: PLC0415

    state = F.load_state(case.uid)
    live = state.get("agent_live")
    if not live and case.uid in A.RUNNING:
        return {"at": F.now(), "mode": "demo", "steps": [], "running": True, "stopped": None, "reason": None, "step": None}
    if live:
        if case.uid in A.RUNNING:
            return live
        return live | {"running": False, "stopped": "error", "reason": "The run was cut off (the server restarted). Run it again."}
    runs = state.get("agent_runs") or []
    return runs[-1] if runs else None
