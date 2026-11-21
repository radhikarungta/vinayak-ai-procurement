"""Vendor directory endpoints.  Include with:  app.include_router(procurement.vendors.api.router)"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from . import store
from .store import PerformanceIn, StoreError, VendorFields

router = APIRouter(prefix="/api/vendors", tags=["vendors"])


def _call(fn, *args):
    try:
        return fn(*args)
    except StoreError as e:
        raise HTTPException(e.code, str(e)) from e


@router.get("")
def list_vendors(category: str | None = None, q: str | None = None, status: str | None = Query(None, description=" | ".join(store.STATUSES))) -> dict:
    if status and status not in store.STATUSES:
        raise HTTPException(400, f"Status should be one of: {', '.join(store.STATUSES)}.")
    items = store.list_vendors(category=category, q=q, status=status)
    return {"count": len(items), "items": [store.public(v) for v in items]}


@router.get("/categories")
def categories() -> list[dict]:
    return store.categories()


@router.get("/summary")
def summary() -> dict:
    """Shape shared by the dashboard tiles: title, headline, stats[{label, value, tone}], ready."""
    vs = store.load_all()
    n = len(vs)
    with_cat = sum(1 for v in vs if v.categories)
    to_confirm = sum(1 for v in vs if v.categories_to_confirm)
    with_contact = sum(1 for v in vs if v.email or v.phone)
    with_history = sum(1 for v in vs if v.performance)
    approved = sum(1 for v in vs if v.status == "approved")
    if not n:
        headline = "No vendors yet. Run the seed or add one by hand."
    else:
        headline = f"{n} vendors. {with_cat} have a category, {with_contact} have contact details, {with_history} have a past-performance record."
    tone = lambda k: None if not n else ("good" if k == n else "warn" if k else "bad")  # noqa: E731
    return {
        "title": "Vendor directory",
        "headline": headline,
        "stats": [
            {"label": "Vendors", "value": n, "tone": None},
            {"label": "Approved", "value": approved, "tone": tone(approved)},
            {"label": "With a category", "value": with_cat, "tone": tone(with_cat)},
            {"label": "Categories to confirm", "value": to_confirm, "tone": ("warn" if to_confirm else "good") if n else None},
            {"label": "With contact details", "value": with_contact, "tone": tone(with_contact)},
            # Missing history is a gap to fill, not a failing, so it's never "bad".
            {"label": "With past performance", "value": with_history, "tone": ("good" if with_history == n else "warn") if n else None},
        ],
        # Ready to draft RFQs: someone in a category, and someone we can actually reach.
        "ready": n > 0 and with_cat > 0 and with_contact > 0,
    }


@router.get("/{vendor_id}")
def get_vendor(vendor_id: str) -> dict:
    return store.public(_call(store.get, vendor_id))


@router.post("", status_code=201)
def create_vendor(body: VendorFields) -> dict:
    return store.public(_call(store.create, body))


@router.patch("/{vendor_id}")
@router.put("/{vendor_id}")
def update_vendor(vendor_id: str, body: VendorFields) -> dict:
    return store.public(_call(store.update, vendor_id, body))


@router.delete("/{vendor_id}", status_code=204)
def delete_vendor(vendor_id: str) -> Response:
    _call(store.delete, vendor_id)
    return Response(status_code=204)


@router.post("/{vendor_id}/performance", status_code=201)
def add_performance(vendor_id: str, body: PerformanceIn) -> dict:
    return store.public(_call(store.add_performance, vendor_id, body))
