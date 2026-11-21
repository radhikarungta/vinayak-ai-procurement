"""Requirement workflow endpoints: email -> requirement -> item codes (reply to site) -> approve ->
vendor scoring and picks -> RFQ drafts. Included by procurement/api.py."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..itemcodes import master as M
from . import flow as F
from . import reply as R
from . import rfq as Q

router = APIRouter(prefix="/api/cases", tags=["cases"])
TITLE = "Requirement workflow"


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
        c = F.build_case(str(r["uid"]), master=m)
        items.append({"uid": c.uid, "subject": c.subject, "sender": c.sender, "date": c.date, "project": c.project,
                      "stage": c.stage, "counts": c.counts, "headline": c.headline, "replies": len(c.replies),
                      "approved": bool(c.approved_at), "picked": sum(len(v) for v in c.vendor_picks.values()),
                      "rfqs_sent": len({r.vendor_id for r in c.rfqs})})
    return {"ready": True, "message": None, "items": items}


@router.get("/summary")
def summary() -> dict:
    data = list_cases()
    if not data["ready"]:
        return {"title": TITLE, "headline": data["message"], "stats": [], "ready": False}
    items = data["items"]
    st = lambda *s: sum(i["stage"] in s for i in items)  # noqa: E731
    need, appr, vend, sent = st("item_codes", "waiting_on_site"), st("approve"), st("vendors", "rfq"), st("rfq_sent")
    return {
        "title": TITLE,
        "headline": f"{len(items)} requirement emails: {need} on item codes, {appr} to approve, {vend} picking vendors or sending RFQs, {sent} with RFQs sent.",
        "stats": [
            {"label": "On item codes", "value": need, "tone": "warn" if need else None},
            {"label": "To approve", "value": appr, "tone": "warn" if appr else None},
            {"label": "Vendors / RFQ", "value": vend, "tone": None},
            {"label": "RFQs sent", "value": sent, "tone": "good" if sent else None},
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
    _case(uid, m)
    _do(R.send, uid, body.to, body.subject, body.body, mark_only=body.mark_only)
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


@router.post("/{uid}/rfq/{vendor_id}")
def send_rfq(uid: str, vendor_id: str, body: MailIn, category: str | None = None) -> dict:
    m = _master()
    _case(uid, m)
    _do(Q.send, uid, vendor_id, body.to, body.subject, body.body, category=category, mark_only=body.mark_only)
    return _full(_case(uid, m), m)
