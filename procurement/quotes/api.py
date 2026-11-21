"""Quote endpoints (step 4, and the re-priced comparison for step 5). Included by procurement/api.py."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException

from . import compare as C
from . import pack

router = APIRouter(prefix="/api/quotes", tags=["quotes"])
TITLE = "Read & normalise quotes"


def _missing() -> str | None:
    f = pack.files()
    if not f["quotes"]:
        return f"No quotation PDFs found in {pack.pack_dir() / '06_quotations'}."
    return None


@router.get("")
def list_quotes() -> dict:
    if msg := _missing():
        return {"ready": False, "message": msg, "items": []}
    cmp = pack.comparison()
    quotes = pack.read_quotes()
    C.compare(cmp.tender, quotes)  # fills codes and quantity basis
    return {"ready": True, "message": None, "count": len(quotes), "items": [q.model_dump() for q in quotes]}


@router.get("/comparison")
def comparison(basis: Literal["tender", "quoted"] = "tender") -> dict:
    """Vendors ranked on the tender quantities (default) or on their own PDF totals ("quoted")."""
    if msg := _missing():
        return {"ready": False, "message": msg}
    cmp = pack.comparison()
    key = "rank_repriced" if basis == "tender" else "rank_as_quoted"
    vendors = sorted(cmp.vendors, key=lambda v: (getattr(v, key) is None, getattr(v, key) or 0, v.vendor.lower()))
    return {"ready": True, "message": None, "basis": basis, **cmp.model_dump(exclude={"vendors"}),
            "vendors": [v.model_dump() for v in vendors]}


@router.get("/seed")
def seed_preview() -> dict:
    """The deals seed the shortlist would read (not written)."""
    if msg := _missing():
        raise HTTPException(404, msg)
    return pack.deals_seed()


@router.post("/sync-deals")
def sync_deals() -> dict:
    """Write data/seed/parsed_quote_deals.json and replace the deals from those PDFs in the deals store."""
    if msg := _missing():
        raise HTTPException(404, msg)
    return pack.sync_deals()


@router.get("/summary")
def summary() -> dict:
    """Dashboard tile: title, headline, stats[{label, value, tone}], ready."""
    if msg := _missing():
        return {"title": TITLE, "headline": msg, "stats": [], "ready": False}
    cmp = pack.comparison()
    vs = cmp.vendors
    n = len(vs)
    read = sum(1 for v in vs if not v.needs_vision)
    vision = n - read
    ranked = sorted((v for v in vs if v.rank_repriced), key=lambda v: v.rank_repriced)
    other_basis = sum(1 for v in vs if v.quantity_basis == "different")
    headline = cmp.insights[0] if cmp.insights else f"{read} of {n} quotes read."
    return {
        "title": TITLE,
        "headline": headline,
        "stats": [
            {"label": "Quotes", "value": n, "tone": None},
            {"label": "Read from text", "value": read, "tone": "good" if read == n else "warn"},
            {"label": "Need vision", "value": vision, "tone": "warn" if vision else "good"},
            {"label": "Priced on other quantities", "value": other_basis, "tone": "warn" if other_basis else "good"},
            {"label": "Not comparable lines", "value": len(cmp.non_comparable), "tone": "warn" if cmp.non_comparable else "good"},
            {"label": "Lowest re-priced", "value": C.inr(ranked[0].repriced_total) if ranked else "–", "tone": None},
        ],
        "ready": read > 0,
    }
