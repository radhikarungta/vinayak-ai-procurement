"""Requirement workflow endpoints: email -> requirement -> item codes (reply to site) -> approve ->
vendor scoring and picks -> RFQ drafts -> compare quotes, choose the vendor -> email the HOD.
Included by procurement/api.py."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..itemcodes import master as M
from . import flow as F
from . import quotes as CQ
from . import reply as R
from . import rfq as Q
from . import site_reply as SR

router = APIRouter(prefix="/api/cases", tags=["cases"])
TITLE = "Requirement workflow"
log = logging.getLogger(__name__)


def _master() -> M.Master:
    try:
        return M.get_master()
    except M.MasterMissing as e:
        raise HTTPException(409, str(e)) from e


def _case(uid: str, m: M.Master) -> F.Case:
    try:
        return F.build_case(uid, master=m)
    except F.CaseError as e:
        raise HTTPException(404, str(e)) from e


def _full(c: F.Case, m: M.Master) -> dict:
    res = F.record(c.uid).get("result") or {}
    email = res.get("email") or {}
    q = res.get("quantities") or None
    return c.model_dump() | {
        "email": {"body": email.get("body", ""), "attachments": email.get("attachments", [])},
        "requirement": res.get("requirement") or {},
        "quantity_sheet": {"filename": q.get("filename"), "lines": len(q.get("lines") or [])} if q else None,
        "draft": R.draft(c).model_dump(),
        "rfq_drafts": [d.model_dump() for d in Q.drafts(c, master=m)],
        "send": R.can_send(),
        "quotes": CQ.view(c),
    }


def _do(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except F.CaseError as e:
        raise HTTPException(400, str(e)) from e
    except R.SendError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("")
def list_cases() -> dict:
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return {"ready": False, "message": str(e), "items": []}
    items = []
    for r in F.records():
        try:
            c = F.build_case(str(r["uid"]), master=m, rec=r)
        except Exception:  # one bad record (or saved state) must not break the list: show it as an error
            log.exception("Requirement %s couldn't be built", r.get("uid"))
            email = (r.get("result") or {}).get("email") or {}
            items.append({"uid": str(r.get("uid")), "subject": email.get("subject"), "sender": email.get("sender"), "date": email.get("date"),
                          "project": None, "stage": "error", "counts": {}, "headline": "This requirement couldn't be opened (see the server log).",
                          "replies": 0, "approved": False, "picked": 0, "rfqs_sent": 0, "quotes_in": 0, "awarded": [], "hod_sent": False})
            continue
        items.append({"uid": c.uid, "subject": c.subject, "sender": c.sender, "date": c.date, "project": c.project,
                      "stage": c.stage, "counts": c.counts, "headline": c.headline, "replies": len(c.replies),
                      "approved": bool(c.approved_at), "picked": sum(len(v) for v in c.vendor_picks.values()),
                      "rfqs_sent": len({r.vendor_id for r in c.rfqs}), "quotes_in": c.quotes_in,
                      "awarded": [a["vendor"] for a in c.award.values()], "hod_sent": bool(c.hod)})
    return {"ready": True, "message": None, "items": items}


@router.get("/summary")
def summary() -> dict:
    data = list_cases()
    if not data["ready"]:
        return {"title": TITLE, "headline": data["message"], "stats": [], "ready": False}
    items = data["items"]
    st = lambda *s: sum(i["stage"] in s for i in items)  # noqa: E731
    need, appr, vend, sent, bad = st("item_codes", "waiting_on_site"), st("approve"), st("vendors", "rfq"), st("rfq_sent", "quotes"), st("error")
    hod, done = st("hod"), st("done")
    return {
        "title": TITLE,
        "headline": f"{len(items)} requirement emails: {need} on item codes, {appr} to approve, {vend} picking vendors or sending RFQs, "
        f"{sent} waiting for or comparing quotes, {hod} with the HOD, {done} approved."
        + (f" {bad} couldn't be opened." if bad else ""),
        "stats": [
            {"label": "On item codes", "value": need, "tone": "warn" if need else None},
            {"label": "To approve", "value": appr, "tone": "warn" if appr else None},
            {"label": "Vendors / RFQ", "value": vend, "tone": None},
            {"label": "Quotes", "value": sent, "tone": None},
            {"label": "With HOD", "value": hod, "tone": "warn" if hod else None},
            {"label": "Approved", "value": done, "tone": "good" if done else None},
        ],
        "ready": True,
    }


@router.get("/{uid}")
def get_case(uid: str) -> dict:
    m = _master()
    return _full(_case(uid, m), m)


class DecisionIn(BaseModel):
    action: Literal["use_code", "skip", "ask_site", "clear"]
    codes: list[str] = []
    note: str | None = None


@router.post("/{uid}/lines/{line_id}")
def decide(uid: str, line_id: str, body: DecisionIn) -> dict:
    m = _master()
    return _full(_do(F.decide, uid, line_id, body.action, body.codes, body.note, master=m), m)


class CheckIn(BaseModel):
    resolved: bool = True


@router.post("/{uid}/checks/{check_id}")
def resolve_check(uid: str, check_id: str, body: CheckIn) -> dict:
    m = _master()
    return _full(_do(F.resolve_check, uid, check_id, body.resolved, master=m), m)


class MailIn(BaseModel):
    to: str
    subject: str
    body: str
    mark_only: bool = False


@router.post("/{uid}/reply")
def send_reply(uid: str, body: MailIn) -> dict:
    m = _master()
    _do(R.send, uid, body.to, body.subject, body.body, mark_only=body.mark_only, case=_case(uid, m))
    return _full(_case(uid, m), m)


class SiteReplyIn(BaseModel):
    body: str
    sender: str | None = None


@router.post("/{uid}/site-reply")
def paste_site_reply(uid: str, body: SiteReplyIn) -> dict:
    """The site's answer pasted in by hand (it came by phone or WhatsApp, or the mail isn't connected)."""
    m = _master()
    _case(uid, m)
    _do(SR.record, uid, body.body, sender=body.sender, via="pasted", master=m)
    return _full(_case(uid, m), m)


@router.post("/{uid}/site-reply/{index}/read")
def reread_site_reply(uid: str, index: int) -> dict:
    m = _master()
    _case(uid, m)
    _do(SR.reread, uid, index, master=m)
    return _full(_case(uid, m), m)


class ApproveIn(BaseModel):
    approved: bool = True


@router.post("/{uid}/approve")
def approve(uid: str, body: ApproveIn) -> dict:
    m = _master()
    return _full(_do(F.approve, uid, body.approved, master=m), m)


@router.get("/{uid}/vendors")
def vendors(uid: str) -> dict:
    m = _master()
    return F.vendors_for(_case(uid, m))


class PicksIn(BaseModel):
    category: str
    vendor_ids: list[str]


@router.post("/{uid}/vendors")
def pick_vendors(uid: str, body: PicksIn) -> dict:
    m = _master()
    return _full(_do(F.pick_vendors, uid, body.category, body.vendor_ids, master=m), m)


class ItemsIn(BaseModel):
    codes: list[str]


@router.post("/{uid}/rfq/{vendor_id}/items")
def rfq_items(uid: str, vendor_id: str, body: ItemsIn, category: str) -> dict:
    m = _master()
    return _full(_do(F.pick_rfq_items, uid, vendor_id, category, body.codes, master=m), m)


@router.post("/{uid}/rfq/{vendor_id}")
def send_rfq(uid: str, vendor_id: str, body: MailIn, category: str | None = None) -> dict:
    m = _master()
    _case(uid, m)
    _do(Q.send, uid, vendor_id, body.to, body.subject, body.body, category=category, mark_only=body.mark_only)
    return _full(_case(uid, m), m)


# ── Steps 6 and 7: quotes, comparison, award, HOD ──────────────────────────────────────────────


@router.get("/{uid}/quotes")
def quotes(uid: str) -> dict:
    m = _master()
    return CQ.view(_case(uid, m))


@router.post("/{uid}/quotes/{vendor_id}")
async def upload_quote(uid: str, vendor_id: str, category: str, file: UploadFile = File(...)) -> dict:
    m = _master()
    _case(uid, m)
    data = await file.read(CQ.MAX_PDF + 1)
    return _full(_do(CQ.upload, uid, vendor_id, category, data, file.filename), m)


class LinkIn(BaseModel):
    use: bool = True


@router.post("/{uid}/quotes/{vendor_id}/link")
def link_quote(uid: str, vendor_id: str, category: str, body: LinkIn) -> dict:
    m = _master()
    return _full(_do(CQ.link, uid, vendor_id, category, body.use), m)


class RatesIn(BaseModel):
    rates: dict[str, float | None]


@router.post("/{uid}/quotes/{vendor_id}/rates")
def quote_rates(uid: str, vendor_id: str, category: str, body: RatesIn) -> dict:
    m = _master()
    return _full(_do(CQ.set_rates, uid, vendor_id, category, body.rates), m)


@router.post("/{uid}/compare")
def compare(uid: str, body: PicksIn) -> dict:
    m = _master()
    return _full(_do(CQ.pick_compare, uid, body.category, body.vendor_ids), m)


class AwardIn(BaseModel):
    category: str
    vendor_id: str | None
    reason: str | None = None


@router.post("/{uid}/award")
def award(uid: str, body: AwardIn) -> dict:
    m = _master()
    return _full(_do(CQ.award, uid, body.category, body.vendor_id, body.reason), m)


@router.post("/{uid}/hod")
def send_hod(uid: str, body: MailIn) -> dict:
    m = _master()
    _case(uid, m)
    _do(CQ.send_hod, uid, body.to, body.subject, body.body, mark_only=body.mark_only)
    return _full(_case(uid, m), m)


@router.post("/{uid}/hod/approve")
def hod_approve(uid: str, body: ApproveIn) -> dict:
    m = _master()
    return _full(_do(CQ.approve_hod, uid, body.approved), m)
