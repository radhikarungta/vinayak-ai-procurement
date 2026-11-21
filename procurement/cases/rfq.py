"""RFQ drafts: one email per picked vendor, for the approved items in that vendor's category.

By default every approved item code in the category goes on each vendor's RFQ; a person can tick a
subset per vendor (state["rfq_items"]["<vendor_id>|<category>"], see flow.pick_rfq_items).

The six standard points are the ones Vinayak puts on every RFQ (from the sample RFQ email in the data
pack). The draft is template text a person edits before sending. Sending uses the same test-inbox
limit as the reply to the site (REPLY_ALLOWED_TO). The site's quantity sheet is attached when its
file was kept at intake (emails read from the mailbox) and the vendor's RFQ asks for every line on it;
otherwise the RFQ doesn't mention one.
"""

from __future__ import annotations

import smtplib
from pathlib import Path

from pydantic import BaseModel

from .. import mailbox
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


class RfqItem(BaseModel):
    code: str
    text: str


class RfqDraft(BaseModel):
    vendor_id: str
    vendor: str
    category: str
    to: str
    subject: str
    body: str
    sent: F.SentRfq | None
    available: list[RfqItem]  # every approved item code in the category
    selected: list[str]  # the codes on this vendor's RFQ
    attachment: str | None  # file name of the quantity sheet that goes with it


def _qty(ln: F.CaseLine) -> str:
    if ln.quantity is None:
        return "quantity to be confirmed"
    q = f"{ln.quantity:,.2f}".rstrip("0").rstrip(".")
    return f"{q} {ln.uom or ''}".strip()


def _item_lines(lines: list[F.CaseLine], erp_text: dict[str, str], keep: set[str] | None = None) -> list[str]:
    out, n = [], 0
    for ln in lines:
        codes = [c for c in ln.codes if keep is None or c in keep]
        for code in codes:
            n += 1
            text = erp_text.get(code) or ln.text
            if len(ln.codes) == 1:
                q = _qty(ln)
            elif len(codes) > 1:
                q = f"{_qty(ln)} in total across {' and '.join(codes)}, split to be confirmed"
            else:  # one code of a line the site gave a single total for
                q = "quantity to be confirmed" + (f" (part of the site's {_qty(ln)})" if ln.quantity is not None else "")
            out.append(f"{n}. {code}: {text}, {q}. Site wording: \"{ln.text}\"")
    return out


def requirement_facts(uid: str) -> dict:
    res = F.record(uid).get("result") or {}
    req = res.get("requirement") or {}
    val = lambda k: ((req.get(k) or {}).get("value") or "").strip()  # noqa: E731
    return {"project": val("project"), "block": val("block"), "work": val("work_type"), "start": val("start"), "finish": val("finish"),
            "sheet": bool((res.get("quantities") or {}).get("lines"))}


def sheet_file(uid: str) -> Path | None:
    """The quantity sheet file from the requirement email, if it was kept (mailbox emails only)."""
    if not uid.startswith("m"):
        return None
    q = (F.record(uid).get("result") or {}).get("quantities") or {}
    return mailbox.saved_attachment(uid[1:], q.get("filename")) if q.get("lines") else None


def covers_sheet(case: F.Case, selected: list[str]) -> bool:
    """Attach the site's quantity sheet only when this RFQ asks for every line on it; a sheet with lines
    the vendor isn't asked to quote would make them price the lot."""
    keep = set(selected)
    return all(ln.codes and set(ln.codes) <= keep for ln in case.lines)


def drafts(case: F.Case, *, master: M.Master | None = None) -> list[RfqDraft]:
    if not case.approved_at:
        return []
    from ..vendors import store as V

    vendors = {v.id: v for v in V.load_all()}
    by_code = (master or M.get_master()).by_code()
    groups, _ = F.approved_groups(case)
    facts = requirement_facts(case.uid)
    sheet = sheet_file(case.uid)
    chosen = F.load_state(case.uid).get("rfq_items") or {}
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
        avail = F.category_codes(lines)
        available = [RfqItem(code=c, text=erp_text.get(c) or next(ln.text for ln in lines if c in ln.codes)) for c in avail]
        subject = f"Seeking quotation for {(facts['work'] or cat).lower()} at {where}"
        when = "".join(f"{label}: {facts[k]}\n" for k, label in (("start", "Needed by / start"), ("finish", "Finish")) if facts[k])
        when = when + "\n" if when else ""
        for vid in ids:
            v = vendors.get(vid)
            if v is None:
                continue
            selected = [c for c in avail if c in chosen.get(F.rfq_key(vid, cat), [])] or avail
            att = sheet if sheet and covers_sheet(case, selected) else None
            spec = "the attached quantity sheet and the points below" if att else "the items and points below"
            body = (
                "Dear Sir,\n\n"
                f"You are requested to kindly submit your commercial quotation for the items below at {where}, "
                f"in accordance with {spec}.\n\n"
                "Items:\n" + "\n".join(_item_lines(lines, erp_text, set(selected))) + "\n\n"
                + when
                + "Please quote your rate per unit for each item, the delivery or start lead time, and your validity.\n\n"
                + "Terms:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(STANDARD_TERMS, 1)) + "\n\n"
                "Thanks & Regards,\nContracts & Planning"
            )
            out.append(RfqDraft(vendor_id=vid, vendor=v.name, category=cat, to=v.email or "", subject=subject, body=body,
                                sent=sent.get((vid, cat)), available=available, selected=selected,
                                attachment=att.name if att else None))
    return out


def send(uid: str, vendor_id: str, to: str, subject: str, body: str, *, category: str | None = None,
         mark_only: bool = False, smtp_factory=smtplib.SMTP_SSL) -> F.SentRfq:
    case = F.build_case(uid)
    d = next((x for x in drafts(case) if x.vendor_id == vendor_id and (category is None or x.category == category)), None)
    if d is None:
        raise R.SendError("That vendor isn't picked for this requirement.", 400)
    files = [p for p in [sheet_file(uid)] if p and d.attachment == p.name]  # same rule as the draft
    addr, via = R.deliver(to, subject, body, mark_only=mark_only, smtp_factory=smtp_factory, attachments=files)
    sent = F.SentRfq(to=addr, subject=subject, body=body, at=F.now(), via=via, vendor_id=vendor_id, vendor=d.vendor, category=d.category,
                     attached=[p.name for p in files] if via == "smtp" else [])
    state = F.load_state(uid)
    state["rfqs"] = [r for r in state.get("rfqs", []) if (r["vendor_id"], r["category"]) != (vendor_id, d.category)] + [sent.model_dump()]
    F.save_state(uid, state)
    return sent
