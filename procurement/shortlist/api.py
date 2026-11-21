"""Vendor shortlist endpoints (step 3, first half). Included by procurement/api.py.

The agent ranks and explains. It never sends an RFQ or picks a vendor: a person ticks who gets it.
Scoring is in score.py (no LLM); past deals (quotes and orders) are in deals.py.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from ..vendors import store as vstore
from . import deals as D
from . import score as S

router = APIRouter(prefix="/api/shortlist", tags=["shortlist"])
TITLE = "Shortlist vendors"


def _call(fn, *args):
    try:
        return fn(*args)
    except D.DealError as e:
        raise HTTPException(e.code, str(e)) from e


def headline(res: S.Shortlist) -> str:
    """One or two plain sentences, from the numbers only."""
    n = len(res.vendors)
    if not n:
        return f"No {res.category} vendors in the directory yet, so there is nobody to rank."
    top = next((v for v in res.vendors if v.score is not None), None)
    out = f"{n} {res.category} vendor{'s' * (n != 1)} ranked, {res.selected} pre-ticked for the RFQ."
    if top:
        out += f" {top.name} leads at {top.score:g} on {round(top.coverage * 100)}% of the weight."
    return out


def _default() -> dict | None:
    reqs = S.requirements()
    return next((r for r in reqs if r["item_codes"]), reqs[0] if reqs else None)


@router.get("")
def shortlist(category: str | None = None, codes: str | None = Query(None, description="Comma-separated item codes; empty = every code on file for the category")) -> dict:
    if not category:
        d = _default()
        if d is None:
            raise HTTPException(404, "No categories yet. Add vendors in the Vendors tab.")
        category = d["category"]
    res = S.shortlist(category, [c for c in (codes or "").split(",") if c.strip()])
    return res.model_dump() | {"headline": headline(res), "min_rfq": S.MIN_RFQ}


@router.get("/requirements")
def requirements() -> list[dict]:
    return S.requirements()


@router.get("/summary")
def summary() -> dict:
    """Dashboard tile: title, headline, stats[{label, value, tone}], ready."""
    d = _default()
    if d is None:
        return {"title": TITLE, "headline": "No vendors or past deals yet.", "stats": [], "ready": False}
    res = S.shortlist(d["category"])
    vs = res.vendors
    n = len(vs)
    priced = sum(1 for v in vs if next(f for f in v.factors if f.key == "price").score is not None)
    cov = round(100 * sum(v.coverage for v in vs) / n) if n else 0
    deals = len(D.load_all())
    return {
        "title": TITLE,
        "headline": headline(res) + (f" Average data coverage {cov}%." if n else ""),
        "stats": [
            {"label": f"{res.category} vendors", "value": n, "tone": "good" if n >= S.MIN_RFQ else "warn"},
            {"label": "Pre-ticked", "value": res.selected, "tone": "good" if res.selected >= S.MIN_RFQ else "warn"},
            {"label": "With price data", "value": priced, "tone": None if priced else "warn"},
            # Low coverage is a gap to fill, not a failing.
            {"label": "Avg. coverage", "value": f"{cov}%", "tone": "good" if cov >= 60 else "warn"},
            {"label": "Deal lines on file", "value": deals, "tone": None if deals else "warn"},
        ],
        "ready": n >= S.MIN_RFQ,
    }


# ── Past deals (quotes and orders) ─────────────────────────────────────────────────────────────
@router.get("/deals")
def list_deals(vendor_id: str | None = None) -> dict:
    names = {v.id: v.name for v in vstore.load_all()}
    items = [d.model_dump() | {"vendor": names.get(d.vendor_id, "Unknown vendor")} for d in D.load_all()
             if vendor_id is None or d.vendor_id == vendor_id]
    items.sort(key=lambda d: (d["vendor"].lower(), d["date_quoted"] or "", d["item_code"] or ""))
    return {"count": len(items), "items": items}


@router.post("/deals", status_code=201)
def add_deal(body: D.DealIn) -> dict:
    return _call(D.create, body).model_dump()


@router.put("/deals/{deal_id}")
def edit_deal(deal_id: str, body: D.DealIn) -> dict:
    return _call(D.update, deal_id, body).model_dump()


@router.delete("/deals/{deal_id}", status_code=204)
def delete_deal(deal_id: str) -> Response:
    _call(D.delete, deal_id)
    return Response(status_code=204)
