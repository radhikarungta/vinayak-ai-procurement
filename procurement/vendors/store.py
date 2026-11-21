"""Vendor directory store: one JSON file (data/vendors.json, git-ignored).

Path is overridable with VENDORS_PATH. The first load seeds the file from the data pack
(see seed.py) when it doesn't exist yet.

Past performance is recorded by people, one record per job, and summarised as plain
averages with the record count. A vendor with no records has "no history": it is never
scored as zero, so a new vendor isn't ranked below a bad one.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = ROOT / "data" / "vendors.json"

Status = Literal["approved", "on list, not vetted", "blocked"]
Disputes = Literal["none", "minor", "major"]
STATUSES: tuple[str, ...] = ("approved", "on list, not vetted", "blocked")

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.RLock()


class StoreError(ValueError):
    """Bad input a person can fix (shown as a 400/404/409)."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def store_path() -> Path:
    return Path(os.environ.get("VENDORS_PATH") or DEFAULT_PATH)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Names ──────────────────────────────────────────────────────────────────────────────────────
_LEGAL = {"pvt", "private", "ltd", "limited", "llp", "co", "company", "inc", "p", "the"}


def name_tokens(name: str) -> list[str]:
    """Lower-case words without punctuation or legal suffixes; runs of single letters are joined
    ("R K CONSTRUCTION" and "RK Construction" both give ["rk", "construction"])."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " ")).split()
    while words and words[-1] in _LEGAL:
        words.pop()
    out: list[str] = []
    prev_single = False
    for w in words:
        single = len(w) == 1 and w.isalpha()
        if single and prev_single:
            out[-1] += w
        else:
            out.append(w)
        prev_single = single
    return out


def norm_name(name: str) -> str:
    return " ".join(name_tokens(name))


# ── Shapes ─────────────────────────────────────────────────────────────────────────────────────
class PerformanceIn(BaseModel):
    date: date
    project: str = Field(min_length=1)
    work: str = Field(min_length=1, description="Work or item supplied, e.g. 'internal waterproofing, toilets'")
    quality: int = Field(ge=1, le=5)
    delivery: int = Field(ge=1, le=5, description="On time and as agreed, 1–5")
    disputes: Disputes = "none"
    note: str = ""
    recorded_by: str = Field(min_length=1, description="Role of the person recording, e.g. 'Site engineer'")


class Performance(PerformanceIn):
    id: str
    recorded_at: str


class VendorFields(BaseModel):
    """Editable fields. Every field is optional so the same shape serves create and update."""

    name: str | None = None
    categories: list[str] | None = None
    categories_to_confirm: list[str] | None = Field(None, description="Categories guessed from the name; a person should confirm them")
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    gstin: str | None = None
    city: str | None = None
    projects: list[str] | None = None
    status: Status | None = None
    notes: str | None = None
    sources: list[str] | None = None

    @field_validator("name", "contact_person", "email", "phone", "gstin", "city", "notes")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v: str | None) -> str | None:
        if not v:
            return v
        v = v.upper().replace(" ", "")
        if not GSTIN_RE.match(v):
            raise ValueError("GSTIN should be 15 characters, like 19ABCDE1234F1Z5.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v and not EMAIL_RE.match(v):
            raise ValueError("That email address doesn't look right.")
        return v

    @field_validator("categories", "categories_to_confirm", "projects", "sources")
    @classmethod
    def _clean_list(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for s in v:
            s = s.strip()
            if s and s.lower() not in {o.lower() for o in out}:
                out.append(s)
        return out


class Vendor(BaseModel):
    id: str
    name: str
    categories: list[str] = []
    categories_to_confirm: list[str] = []
    contact_person: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    city: str = ""
    projects: list[str] = []
    status: Status = "on list, not vetted"
    notes: str = ""
    sources: list[str] = []
    performance: list[Performance] = []
    created_at: str
    updated_at: str


def performance_summary(records: list[Performance] | list[dict]) -> dict:
    """Averages over the records, with the count. Empty → 'no history' and null scores (never 0)."""
    recs = [r if isinstance(r, dict) else r.model_dump() for r in records]
    n = len(recs)
    disputes = {k: sum(1 for r in recs if r["disputes"] == k) for k in ("none", "minor", "major")}
    if not n:
        return {"count": 0, "quality": None, "delivery": None, "disputes": disputes, "label": "no history"}
    q = round(sum(r["quality"] for r in recs) / n, 1)
    d = round(sum(r["delivery"] for r in recs) / n, 1)
    bits = [f"Quality {q}/5", f"Delivery {d}/5"]
    if disputes["major"] or disputes["minor"]:
        bits.append(", ".join(f"{disputes[k]} {k} dispute{'s' * (disputes[k] != 1)}" for k in ("major", "minor") if disputes[k]))
    return {"count": n, "quality": q, "delivery": d, "disputes": disputes,
            "label": " · ".join(bits) + f" ({n} record{'s' * (n != 1)})"}


def public(v: Vendor) -> dict:
    out = v.model_dump(mode="json")
    out["performance_summary"] = performance_summary(v.performance)
    out["has_contact"] = bool(v.email or v.phone)
    return out


# ── File I/O ───────────────────────────────────────────────────────────────────────────────────
def _read_raw() -> list[dict]:
    path = store_path()
    if not path.exists():
        from . import seed  # lazy: seed imports this module

        seed.run(quiet=True)
        if not path.exists():
            return []
    data = json.loads(path.read_text() or "{}")
    return data.get("vendors", [])


def load_all() -> list[Vendor]:
    with _lock:
        return [Vendor.model_validate(v) for v in _read_raw()]


def save_all(vendors: list[Vendor]) -> None:
    path = store_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"version": 1, "vendors": [v.model_dump(mode="json") for v in vendors]}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)


# ── Queries ────────────────────────────────────────────────────────────────────────────────────
def list_vendors(category: str | None = None, q: str | None = None, status: str | None = None) -> list[Vendor]:
    out = load_all()
    if category:
        c = category.strip().lower()
        out = [v for v in out if c in {x.lower() for x in v.categories}]
    if status:
        out = [v for v in out if v.status == status]
    if q:
        needle = q.strip().lower()
        hay = lambda v: " ".join([v.name, v.contact_person, v.email, v.phone, v.gstin, v.city, v.notes, *v.categories, *v.projects, *v.sources]).lower()  # noqa: E731
        out = [v for v in out if needle in hay(v) or (norm_name(q) and norm_name(q) in norm_name(v.name))]
    return sorted(out, key=lambda v: v.name.lower())


def get(vendor_id: str) -> Vendor:
    for v in load_all():
        if v.id == vendor_id:
            return v
    raise StoreError("No vendor with that id.", 404)


def find_by_name(vendors: list[Vendor], name: str) -> Vendor | None:
    key = norm_name(name)
    return next((v for v in vendors if norm_name(v.name) == key), None)


def categories() -> list[dict]:
    counts: dict[str, dict] = {}
    for v in load_all():
        for c in v.categories:
            e = counts.setdefault(c.lower(), {"name": c, "vendors": 0, "to_confirm": 0})
            e["vendors"] += 1
            e["to_confirm"] += c in v.categories_to_confirm
    return sorted(counts.values(), key=lambda e: (-e["vendors"], e["name"].lower()))


# ── Writes ─────────────────────────────────────────────────────────────────────────────────────
def _apply(v: Vendor, f: VendorFields) -> None:
    data = f.model_dump(exclude_unset=True)
    for k, val in data.items():
        if k == "name" and not val:
            raise StoreError("A vendor needs a name.")
        setattr(v, k, val if val is not None else ([] if isinstance(getattr(v, k), list) else ""))
    # A category to confirm must still be a category.
    v.categories_to_confirm = [c for c in v.categories_to_confirm if c in v.categories]


def create(f: VendorFields) -> Vendor:
    if not f.name:
        raise StoreError("A vendor needs a name.")
    with _lock:
        vendors = load_all()
        if (dup := find_by_name(vendors, f.name)) is not None:
            raise StoreError(f"{dup.name} is already in the directory.", 409)
        t = now()
        v = Vendor(id=uuid.uuid4().hex[:12], name=f.name, created_at=t, updated_at=t)
        _apply(v, f)
        vendors.append(v)
        save_all(vendors)
        return v


def update(vendor_id: str, f: VendorFields) -> Vendor:
    with _lock:
        vendors = load_all()
        v = next((x for x in vendors if x.id == vendor_id), None)
        if v is None:
            raise StoreError("No vendor with that id.", 404)
        if f.name and (dup := find_by_name(vendors, f.name)) is not None and dup.id != v.id:
            raise StoreError(f"{dup.name} is already in the directory.", 409)
        _apply(v, f)
        v.updated_at = now()
        save_all(vendors)
        return v


def delete(vendor_id: str) -> None:
    with _lock:
        vendors = load_all()
        keep = [v for v in vendors if v.id != vendor_id]
        if len(keep) == len(vendors):
            raise StoreError("No vendor with that id.", 404)
        save_all(keep)


def add_performance(vendor_id: str, rec: PerformanceIn) -> Vendor:
    with _lock:
        vendors = load_all()
        v = next((x for x in vendors if x.id == vendor_id), None)
        if v is None:
            raise StoreError("No vendor with that id.", 404)
        v.performance.append(Performance(**rec.model_dump(), id=uuid.uuid4().hex[:10], recorded_at=now()))
        v.performance.sort(key=lambda r: r.date, reverse=True)
        if rec.project and rec.project.lower() not in {p.lower() for p in v.projects}:
            v.projects.append(rec.project)
        v.updated_at = now()
        save_all(vendors)
        return v
