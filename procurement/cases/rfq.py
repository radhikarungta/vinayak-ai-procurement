"""RFQ drafts: one email per picked vendor, for the approved items in that vendor's category.

The six standard points are the ones Vinayak puts on every RFQ (from the sample RFQ email in the data
pack). The draft is template text a person edits before sending. Sending uses the same test-inbox
limit as the reply to the site (REPLY_ALLOWED_TO).
"""

from __future__ import annotations

import smtplib

from pydantic import BaseModel

from ..itemcodes import master as M
from . import flow as F
from . import reply as R

STANDARD_TERMS = [
    "PF & ESI registration required.",
    "TDS will be deducted as per Govt. norms.",
    "5% Security deposit will be released after 12 months of total completion of work.",
    "GST will be paid extra.",
    "Water & Electricity at a point and Labour hutment will be provided.",
    "Safety PPE cost will be under Contractor's scope.",
]


class RfqDraft(BaseModel):
    vendor_id: str
    vendor: str
    category: str
    to: str
    subject: str
    body: str
    sent: F.SentRfq | None


def _qty(ln: F.CaseLine) -> str:
    if ln.quantity is None:
        return "quantity to be confirmed"
    q = f"{ln.quantity:,.2f}".rstrip("0").rstrip(".")
    return f"{q} {ln.uom or ''}".strip()


def _item_lines(lines: list[F.CaseLine], erp_text: dict[str, str]) -> list[str]:
    out, n = [], 0
    for ln in lines:
        for code in ln.codes:
            n += 1
            text = erp_text.get(code) or ln.text
            q = _qty(ln) if len(ln.codes) == 1 else f"{_qty(ln)} in total across {' and '.join(ln.codes)}, split to be confirmed"
            out.append(f"{n}. {code}: {text}, {q}. Site wording: \"{ln.text}\"")
    return out


def requirement_facts(uid: str) -> dict:
    res = F.record(uid).get("result") or {}
    req = res.get("requirement") or {}
    val = lambda k: ((req.get(k) or {}).get("value") or "").strip()  # noqa: E731
    return {"project": val("project"), "block": val("block"), "work": val("work_type"), "start": val("start"), "finish": val("finish"),
            "sheet": bool((res.get("quantities") or {}).get("lines"))}


def drafts(case: F.Case, *, master: M.Master | None = None) -> list[RfqDraft]:
    if not case.approved_at:
        return []
    from ..vendors import store as V

    vendors = {v.id: v for v in V.load_all()}
    by_code = (master or M.get_master()).by_code()
    groups, _ = F.approved_groups(case)
    facts = requirement_facts(case.uid)
    where = ", ".join(x for x in (facts["block"], facts["project"]) if x) or "our project"
    sent = {(r.vendor_id, r.category): r for r in case.rfqs}
    out = []
    for cat, ids in case.vendor_picks.items():
        lines = groups.get(cat, [])
        if not lines:
            continue
        erp_text = {c: es[0].title for c, es in by_code.items()}
        for ln in lines:
            if ln.match.suggestion and ln.match.suggestion.code in ln.codes:
                erp_text.setdefault(ln.match.suggestion.code, "new item code")
        subject = f"Seeking quotation for {(facts['work'] or cat).lower()} at {where}"
        when = "".join(f"{label}: {facts[k]}\n" for k, label in (("start", "Needed by / start"), ("finish", "Finish")) if facts[k])
        when = when + "\n" if when else ""
        spec = "the attached quantity sheet and the points below" if facts["sheet"] else "the points below"
        for vid in ids:
            v = vendors.get(vid)
            if v is None:
                continue
            body = (
                "Dear Sir,\n\n"
                f"You are requested to kindly submit your commercial quotation for the items below at {where}, "
                f"in accordance with {spec}.\n\n"
                "Items:\n" + "\n".join(_item_lines(lines, erp_text)) + "\n\n"
                + when
                + "Please quote your rate per unit for each item, the delivery or start lead time, and your validity.\n\n"
                + "Terms:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(STANDARD_TERMS, 1)) + "\n\n"
                "Thanks & Regards,\nContracts & Planning"
            )
            out.append(RfqDraft(vendor_id=vid, vendor=v.name, category=cat, to=v.email or "", subject=subject, body=body, sent=sent.get((vid, cat))))
    return out


def send(uid: str, vendor_id: str, to: str, subject: str, body: str, *, category: str | None = None,
         mark_only: bool = False, smtp_factory=smtplib.SMTP_SSL) -> F.SentRfq:
    case = F.build_case(uid)
    d = next((x for x in drafts(case) if x.vendor_id == vendor_id and (category is None or x.category == category)), None)
    if d is None:
        raise R.SendError("That vendor isn't picked for this requirement.", 400)
    addr, via = R.deliver(to, subject, body, mark_only=mark_only, smtp_factory=smtp_factory)
    sent = F.SentRfq(to=addr, subject=subject, body=body, at=F.now(), via=via, vendor_id=vendor_id, vendor=d.vendor, category=d.category)
    state = F.load_state(uid)
    state["rfqs"] = [r for r in state.get("rfqs", []) if (r["vendor_id"], r["category"]) != (vendor_id, d.category)] + [sent.model_dump()]
    F.save_state(uid, state)
    return sent
