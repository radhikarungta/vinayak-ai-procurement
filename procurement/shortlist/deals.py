"""Past deals with vendors: quotes and orders, one record per item line.

One JSON file, data/vendor_deals.json (git-ignored), overridable with VENDOR_DEALS_PATH.

When the file doesn't exist yet it is seeded from a hand transcription of real quotations: the
first data/seed/*quotes*.json (also git-ignored; override with VENDOR_DEALS_SEED). This code
holds no real names or rates: if no seed file is there, the store simply starts empty.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..vendors import store as vstore

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = ROOT / "data" / "vendor_deals.json"
SEED_DIR = ROOT / "data" / "seed"

_lock = threading.RLock()


class DealError(ValueError):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def store_path() -> Path:
    return Path(os.environ.get("VENDOR_DEALS_PATH") or DEFAULT_PATH)


def seed_path() -> Path | None:
    if p := os.environ.get("VENDOR_DEALS_SEED"):
        return Path(p)
    found = sorted(SEED_DIR.glob("*quotes*.json")) if SEED_DIR.is_dir() else []
    return found[0] if found else None


def norm_code(code: str | None) -> str | None:
    return code.strip().upper() if code and code.strip() else None


def norm_unit(unit: str) -> str:
    u = unit.strip().lower().replace(".", "").replace(" ", "")
    return {"sft": "sqft", "sqfeet": "sqft", "rm": "rmt", "rmtr": "rmt", "m2": "sqm", "sqmtr": "sqm"}.get(u, u)


def _as_datetime(s: str | None) -> datetime | None:
    """A date alone counts as the end of that day (we don't know when in the day it came)."""
    if not s:
        return None
    s = s.strip()
    if len(s) == 10:
        return datetime.combine(date.fromisoformat(s), time(23, 59))
    return datetime.fromisoformat(s).replace(tzinfo=None)


def response_hours(rfq_sent_at: str | None, date_quoted: str | None) -> float | None:
    a, b = _as_datetime(rfq_sent_at), _as_datetime(date_quoted)
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() / 3600, 1)


# ── Shapes ─────────────────────────────────────────────────────────────────────────────────────
class DealIn(BaseModel):
    vendor_id: str = Field(min_length=1)
    kind: Literal["quote", "order"] = "quote"
    project: str = ""
    category: str = Field(min_length=1)
    item_code: str | None = None
    item_text: str = ""
    unit: str = Field(min_length=1, description="sqft, rmt, sqm, bag, MT …")
    rate: float = Field(gt=0, description="Rupees per unit, before GST")
    date_quoted: str | None = Field(None, description="YYYY-MM-DD, or a full date-time")
    rfq_sent_at: str | None = Field(None, description="When we sent the RFQ (date-time)")
    lead_time_days_promised: int | None = Field(None, ge=0)
    lead_time_days_actual: int | None = Field(None, ge=0, description="Orders only")
    payment_terms: str = ""
    gst: str = ""
    validity_days: int | None = Field(None, ge=0)
    source: str = ""
    notes: str = ""

    @field_validator("item_code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        return norm_code(v)

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        return norm_unit(v)

    @field_validator("date_quoted", "rfq_sent_at")
    @classmethod
    def _dt(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        try:
            _as_datetime(v)
        except ValueError as e:
            raise ValueError("Use a date like 2026-11-17, or a date and time like 2026-11-16T18:14.") from e
        return str(v).strip()

    @field_validator("project", "category", "item_text", "payment_terms", "gst", "source", "notes")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _actual_only_on_orders(self):
        if self.kind == "quote" and self.lead_time_days_actual is not None:
            raise ValueError("Actual lead time only applies to orders.")
        return self


class Deal(DealIn):
    id: str
    response_hours: float | None = None


def _finish(d: DealIn, deal_id: str) -> Deal:
    return Deal(**d.model_dump(), id=deal_id, response_hours=response_hours(d.rfq_sent_at, d.date_quoted))


# ── File I/O ───────────────────────────────────────────────────────────────────────────────────
def load_all() -> list[Deal]:
    with _lock:
        path = store_path()
        if not path.exists():
            deals = seed_deals()
            if deals:
                save_all(deals)
            return deals
        data = json.loads(path.read_text() or "{}")
        return [Deal.model_validate(x) for x in data.get("deals", [])]


def save_all(deals: list[Deal]) -> None:
    path = store_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "deals": [d.model_dump(mode="json") for d in deals]}, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)


def create(d: DealIn) -> Deal:
    with _lock:
        _check_vendor(d.vendor_id)
        deals = load_all()
        deal = _finish(d, uuid.uuid4().hex[:10])
        deals.append(deal)
        save_all(deals)
        return deal


def update(deal_id: str, d: DealIn) -> Deal:
    with _lock:
        _check_vendor(d.vendor_id)
        deals = load_all()
        for i, x in enumerate(deals):
            if x.id == deal_id:
                deals[i] = _finish(d, deal_id)
                save_all(deals)
                return deals[i]
        raise DealError("No deal with that id.", 404)


def delete(deal_id: str) -> None:
    with _lock:
        deals = load_all()
        keep = [x for x in deals if x.id != deal_id]
        if len(keep) == len(deals):
            raise DealError("No deal with that id.", 404)
        save_all(keep)


def for_vendor(vendor_id: str) -> list[Deal]:
    return [d for d in load_all() if d.vendor_id == vendor_id]


def _check_vendor(vendor_id: str) -> None:
    if not any(v.id == vendor_id for v in vstore.load_all()):
        raise DealError("No vendor with that id. Add the vendor in the Vendors tab first.", 404)


# ── Seed ───────────────────────────────────────────────────────────────────────────────────────
def match_vendor(vendors: list[vstore.Vendor], name: str) -> vstore.Vendor | None:
    """Same normalised name, or the directory name is the start of the quote's name
    ("Riverbank Builders" for "Riverbank Builders & Waterproofing")."""
    if (v := vstore.find_by_name(vendors, name)) is not None:
        return v
    toks = vstore.name_tokens(name)
    hits = [v for v in vendors if (vt := vstore.name_tokens(v.name)) and toks[: len(vt)] == vt]
    return max(hits, key=lambda v: len(vstore.name_tokens(v.name))) if hits else None


def seed_deals(path: Path | None = None) -> list[Deal]:
    """Deals from the seed transcription (see the module docstring). Unknown vendors are skipped."""
    path = path or seed_path()
    if path is None or not path.exists():
        return []
    data = json.loads(path.read_text())
    vendors = vstore.load_all()
    lines = data.get("lines", {})
    out: list[Deal] = []
    for q in data.get("quotes", []):
        v = match_vendor(vendors, q["vendor_name"])
        if v is None:
            continue
        source = " · ".join(x for x in (q.get("file"), q.get("ref")) if x)
        for r in q.get("rates", []):
            code = norm_code(r.get("item_code"))
            d = DealIn(
                vendor_id=v.id, kind=q.get("kind", "quote"), project=data.get("project", ""), category=data.get("category", ""),
                item_code=code, item_text=r.get("item_text") or lines.get(code or "", ""), unit=r["unit"], rate=r["rate"],
                date_quoted=q.get("date_quoted"), rfq_sent_at=q.get("rfq_sent_at"),
                lead_time_days_promised=q.get("lead_time_days_promised"), lead_time_days_actual=q.get("lead_time_days_actual"),
                payment_terms=q.get("payment_terms", ""), gst=q.get("gst", ""), validity_days=q.get("validity_days"),
                source=source, notes=" ".join(x for x in (q.get("notes", ""), r.get("note", "")) if x),
            )
            out.append(_finish(d, uuid.uuid4().hex[:10]))
    return out
