"""Score and shortlist vendors for a requirement. Plain arithmetic, no LLM.

A requirement here is a category (e.g. "Waterproofing") and, optionally, the item codes it needs.
Candidates are vendors in that category, plus anyone with a past deal on one of the codes.
Blocked vendors are listed as excluded, with the reason.

Five factors, each scored 0–1 with a one-line reason:

| factor       | weight | data                                   | 1.0 when …              | 0.0 when …            |
|--------------|--------|----------------------------------------|-------------------------|-----------------------|
| price        | 0.35   | latest rate per item code vs median    | 20% or more below median| 20% or more above     |
| performance  | 0.25   | quality + delivery 1–5, disputes       | 5/5, no disputes        | 1/5                   |
| lead time    | 0.15   | days (actual on orders, else promised) | 7 days or less          | 45 days or more       |
| response     | 0.15   | hours from RFQ sent to quote           | 24 h or less            | 7 days or more        |
| experience   | 0.10   | past jobs + projects quoted with us    | 3 or more               | (1 job = 0.6)         |

The weights are assumptions (SOP 05 only says: float the tender to at least three eligible,
relevant vendors). A factor with no data is "no data", never 0: it is left out and the other
weights are re-normalised. Coverage is the share of total weight that had data, so a high score on
thin data is visible as such.

Ranking: score (higher first), then coverage, then name. Vendors with the same score and coverage
share a rank. The top three are pre-ticked (SOP 05: at least three); if a tie straddles third
place, every tied vendor is ticked. A person changes the ticks.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

from pydantic import BaseModel

from ..vendors import store as vstore
from . import deals as dstore

WEIGHTS: dict[str, float] = {"price": 0.35, "performance": 0.25, "lead_time": 0.15, "response": 0.15, "experience": 0.10}
LABELS = {"price": "Price", "performance": "Past performance", "lead_time": "Lead time", "response": "Response time", "experience": "Experience"}
MIN_RFQ = 3  # SOP 05


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear(x: float, best: float, worst: float) -> float:
    """1 at `best` or better, 0 at `worst` or worse, straight line between."""
    return _clamp((worst - x) / (worst - best))


def _num(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


# ── Shapes ─────────────────────────────────────────────────────────────────────────────────────
class Factor(BaseModel):
    key: str
    label: str
    weight: float
    score: float | None  # 0–1; None = no data
    reason: str


class Scored(BaseModel):
    vendor_id: str
    name: str
    status: str
    categories: list[str]
    score: float | None  # 0–100 over the factors with data; None when no factor had data
    coverage: float  # 0–1, share of weight with data
    rank: int = 0
    tied: bool = False
    selected: bool = False
    factors: list[Factor]
    notes: list[str] = []


class Excluded(BaseModel):
    vendor_id: str
    name: str
    reason: str


class Shortlist(BaseModel):
    category: str
    item_codes: list[str]
    medians: dict[str, dict]  # code -> {unit, median, quotes}
    weights: dict[str, float]
    vendors: list[Scored]
    excluded: list[Excluded]
    selected: int
    notes: list[str]


# ── Factors ────────────────────────────────────────────────────────────────────────────────────
def latest_rates(deals: list[dstore.Deal]) -> dict[tuple[str, str], dstore.Deal]:
    """(vendor_id, code) -> the vendor's most recent deal for that code. Undated counts as oldest."""
    out: dict[tuple[str, str], dstore.Deal] = {}
    for d in deals:
        if not d.item_code:
            continue
        k = (d.vendor_id, d.item_code)
        if k not in out or (d.date_quoted or "") >= (out[k].date_quoted or ""):
            out[k] = d
    return out


def medians(latest: dict[tuple[str, str], dstore.Deal], codes: list[str]) -> dict[str, dict]:
    """Median of vendors' latest rates per code, in the unit most of them quoted in.
    Needs at least two vendors in that unit; with one there's nothing to compare against."""
    out: dict[str, dict] = {}
    for code in codes:
        by_unit: dict[str, list[float]] = defaultdict(list)
        for (_, c), d in latest.items():
            if c == code:
                by_unit[d.unit].append(d.rate)
        if not by_unit:
            continue
        unit, rates = max(by_unit.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if len(rates) >= 2:
            out[code] = {"unit": unit, "median": round(median(rates), 2), "quotes": len(rates)}
    return out


def price_factor(vendor_id: str, codes: list[str], latest: dict, meds: dict[str, dict]) -> Factor:
    ratios: list[tuple[str, float, float, dict]] = []
    off_unit: list[str] = []
    for code in codes:
        d = latest.get((vendor_id, code))
        if d is None or code not in meds:
            continue
        m = meds[code]
        if d.unit != m["unit"]:
            off_unit.append(f"{code} quoted per {d.unit}, others per {m['unit']}")
            continue
        ratios.append((code, d.rate / m["median"], d.rate, m))
    w = WEIGHTS["price"]
    if not ratios:
        why = "No rate on these item codes that can be compared with other vendors."
        if off_unit:
            why += " " + "; ".join(off_unit) + "."
        return Factor(key="price", label=LABELS["price"], weight=w, score=None, reason=why)
    # Score each code on its own (20% below median = 1, at median = 0.5, 20% above = 0), then average.
    score = sum(_linear(r, 0.8, 1.2) for _, r, _, _ in ratios) / len(ratios)
    avg = sum(r for _, r, _, _ in ratios) / len(ratios)
    pct = round((avg - 1) * 100)
    where = "at the median" if pct == 0 else f"{abs(pct)}% {'below' if pct < 0 else 'above'} the median"
    of = f"on {len(ratios)} of {len(codes)} item code{'s' * (len(codes) != 1)}"
    if len(ratios) == 1:
        code, _, rate, m = ratios[0]
        of += f" (₹{_num(rate)} vs ₹{_num(m['median'])}/{m['unit']})"
    why = f"On average {where} {of}."
    if off_unit:
        why += " Not compared: " + "; ".join(off_unit) + "."
    return Factor(key="price", label=LABELS["price"], weight=w, score=round(score, 3), reason=why)


def performance_factor(v: vstore.Vendor) -> Factor:
    s = vstore.performance_summary(v.performance)
    w = WEIGHTS["performance"]
    if not s["count"]:
        return Factor(key="performance", label=LABELS["performance"], weight=w, score=None, reason="No past-performance record yet.")
    base = ((s["quality"] + s["delivery"]) / 2 - 1) / 4
    penalty = 0.2 * s["disputes"]["major"] + 0.05 * s["disputes"]["minor"]
    return Factor(key="performance", label=LABELS["performance"], weight=w, score=round(_clamp(base - penalty), 3), reason=s["label"] + ".")


def lead_time_factor(vdeals: list[dstore.Deal]) -> Factor:
    w = WEIGHTS["lead_time"]
    actual = [d.lead_time_days_actual for d in vdeals if d.lead_time_days_actual is not None]
    promised = [d.lead_time_days_promised for d in vdeals if d.lead_time_days_promised is not None]
    if actual:
        days, what = median(actual), "delivered in"
    elif promised:
        days, what = median(promised), "promised"
    else:
        return Factor(key="lead_time", label=LABELS["lead_time"], weight=w, score=None, reason="No lead time stated on any quote or order.")
    return Factor(key="lead_time", label=LABELS["lead_time"], weight=w, score=round(_linear(days, 7, 45), 3),
                  reason=f"{what.capitalize()} {_num(days)} days (median).")


def response_factor(vdeals: list[dstore.Deal]) -> Factor:
    w = WEIGHTS["response"]
    # One response per quote, not per item line.
    per_quote = {(d.rfq_sent_at, d.date_quoted): d.response_hours for d in vdeals if d.response_hours is not None}
    if not per_quote:
        return Factor(key="response", label=LABELS["response"], weight=w, score=None, reason="No RFQ send time recorded, so response time is unknown.")
    h = median(per_quote.values())
    n = len(per_quote)
    return Factor(key="response", label=LABELS["response"], weight=w, score=round(_linear(h, 24, 168), 3),
                  reason=f"Quoted {_num(h)} hours after the RFQ ({'median of ' + str(n) + ' quotes' if n > 1 else '1 quote'}).")


def experience_factor(v: vstore.Vendor, vdeals: list[dstore.Deal], category: str) -> Factor:
    w = WEIGHTS["experience"]
    cat = category.lower()
    jobs = len(v.performance)
    projects = {d.project.lower() for d in vdeals if d.project and d.category.lower() == cat}
    n = jobs + len(projects)
    if not n:
        return Factor(key="experience", label=LABELS["experience"], weight=w, score=None, reason="No past jobs or quotes with us in this category.")
    bits = []
    if jobs:
        bits.append(f"{jobs} recorded job{'s' * (jobs != 1)}")
    if projects:
        bits.append(f"quoted on {len(projects)} project{'s' * (len(projects) != 1)}")
    return Factor(key="experience", label=LABELS["experience"], weight=w, score=round(min(1.0, 0.4 + 0.2 * n), 3), reason=" and ".join(bits).capitalize() + ".")


def combine(factors: list[Factor]) -> tuple[float | None, float]:
    """Weighted mean over factors with data, re-normalised; coverage = share of weight with data."""
    total = sum(f.weight for f in factors)
    have = [f for f in factors if f.score is not None]
    wsum = sum(f.weight for f in have)
    coverage = round(wsum / total, 3) if total else 0.0
    if not wsum:
        return None, coverage
    return round(100 * sum(f.weight * f.score for f in have) / wsum, 1), coverage


# ── Ranking ────────────────────────────────────────────────────────────────────────────────────
def rank(items: list[Scored], pick: int = MIN_RFQ) -> list[Scored]:
    """Sort, give tied vendors the same rank, and pre-tick the top `pick` (all of a tie at the cut)."""
    key = lambda s: (s.score is None, -(s.score or 0), -s.coverage)  # noqa: E731
    items = sorted(items, key=lambda s: (*key(s), s.name.lower()))
    for i, s in enumerate(items):
        same_prev = i > 0 and key(items[i - 1]) == key(s)
        same_next = i + 1 < len(items) and key(items[i + 1]) == key(s)
        s.rank = items[i - 1].rank if same_prev else i + 1
        s.tied = (same_prev or same_next) and s.score is not None
    for s in items:
        # Ticked if in the top `pick` places, or tied with someone who is. Never tick "no data".
        s.selected = s.score is not None and s.rank <= pick
    return items


def shortlist(category: str, item_codes: list[str] | None = None, *, vendors: list[vstore.Vendor] | None = None,
              deals: list[dstore.Deal] | None = None) -> Shortlist:
    vendors = vstore.load_all() if vendors is None else vendors
    deals = dstore.load_all() if deals is None else deals
    cat = category.strip().lower()
    cat_deals = [d for d in deals if d.category.lower() == cat]
    codes = [c for c in (dstore.norm_code(c) for c in (item_codes or [])) if c]
    if not codes:
        codes = sorted({d.item_code for d in cat_deals if d.item_code})
    code_set = set(codes)
    relevant = [d for d in deals if d.category.lower() == cat or (d.item_code in code_set)]
    latest = latest_rates([d for d in relevant if d.item_code in code_set])
    meds = medians(latest, codes)

    with_deals = {d.vendor_id for d in relevant}
    scored: list[Scored] = []
    excluded: list[Excluded] = []
    for v in vendors:
        in_cat = cat in {c.lower() for c in v.categories}
        if not in_cat and v.id not in with_deals:
            continue
        if v.status == "blocked":
            excluded.append(Excluded(vendor_id=v.id, name=v.name, reason="Blocked in the vendor directory."))
            continue
        vdeals = [d for d in relevant if d.vendor_id == v.id]
        factors = [
            price_factor(v.id, codes, latest, meds),
            performance_factor(v),
            lead_time_factor(vdeals),
            response_factor(vdeals),
            experience_factor(v, vdeals, category),
        ]
        score, coverage = combine(factors)
        notes = []
        if not in_cat:
            notes.append(f"Not listed under {category} in the directory, but has quoted these item codes.")
        elif category.lower() in {c.lower() for c in v.categories_to_confirm}:
            notes.append(f"{category} was guessed from the name; confirm it.")
        if v.status != "approved":
            notes.append(f"Directory status: {v.status}.")
        if not (v.email or v.phone):
            notes.append("No contact details, so an RFQ can't be sent yet.")
        scored.append(Scored(vendor_id=v.id, name=v.name, status=v.status, categories=v.categories,
                             score=score, coverage=coverage, factors=factors, notes=notes))

    ranked = rank(scored)
    n_sel = sum(s.selected for s in ranked)
    notes = []
    eligible = len(ranked)
    if eligible < MIN_RFQ:
        notes.append(f"SOP 05 asks for at least {MIN_RFQ} eligible vendors; only {eligible} found for {category}. Add vendors in the Vendors tab.")
    elif n_sel < MIN_RFQ:
        notes.append(f"Only {n_sel} vendor{'s' * (n_sel != 1)} have any data to score, so fewer than {MIN_RFQ} are pre-ticked. Tick more by hand.")
    if n_sel > MIN_RFQ:
        notes.append(f"{n_sel} vendors are pre-ticked because of a tie at place {MIN_RFQ}.")
    missing = [c for c in codes if c not in meds]
    if missing:
        notes.append(f"No price median for {', '.join(missing)}: fewer than two comparable rates.")
    return Shortlist(category=category, item_codes=codes, medians=meds, weights=WEIGHTS, vendors=ranked,
                     excluded=excluded, selected=n_sel, notes=notes)


def requirements(deals: list[dstore.Deal] | None = None, vendors: list[vstore.Vendor] | None = None) -> list[dict]:
    """What can be shortlisted: each category (from the directory and past deals) with its known item codes."""
    deals = dstore.load_all() if deals is None else deals
    vendors = vstore.load_all() if vendors is None else vendors
    cats: dict[str, dict] = {}
    for v in vendors:
        for c in v.categories:
            cats.setdefault(c.lower(), {"category": c, "vendors": 0, "item_codes": {}})["vendors"] += 1
    for d in deals:
        e = cats.setdefault(d.category.lower(), {"category": d.category, "vendors": 0, "item_codes": {}})
        if d.item_code:
            e["item_codes"].setdefault(d.item_code, d.item_text)
    out = [{"category": e["category"], "vendors": e["vendors"],
            "item_codes": [{"code": k, "text": t} for k, t in sorted(e["item_codes"].items())]} for e in cats.values()]
    return sorted(out, key=lambda e: (-len(e["item_codes"]), -e["vendors"], e["category"].lower()))
