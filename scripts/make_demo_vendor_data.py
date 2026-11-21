"""Fill the vendor directory with demo history so the shortlist scoring has something to show.

Adds made-up credit terms, past-performance records and quotes/orders for:
  * every vendor in a category the demo requirement emails need (DEMO_REQS below, mirroring the
    cases in samples/demo_inbox.json and their item codes in the ERP item master), plus a few
    uncategorised vendors put into those categories so each has enough vendors to rank;
  * every other vendor that already has a category (credit terms and performance only).

Nothing here is real, and every value is tagged so it can be stripped again:
  * deals and performance records get  source: "demo";
  * on a vendor, made-up fields are listed in  demo_fields  and added categories in  demo_categories.
Real quotes are never changed, and no demo quote is added on an item code a vendor already
quoted for real. The script holds no vendor names: it reads ids from the vendor file at runtime.

Deterministic: every random draw is seeded by the vendor id, so a rerun gives the same data
(any earlier demo data is stripped first). Both files are backed up to *.bak-<timestamp> first.

  uv run python scripts/make_demo_vendor_data.py            # add (or refresh) demo data
  uv run python scripts/make_demo_vendor_data.py --remove   # strip all demo data
  options: --vendors PATH --deals PATH  (default: $VENDORS_PATH / $VENDOR_DEALS_PATH, else data/)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from procurement.shortlist import deals as D  # noqa: E402
from procurement.shortlist import score as S  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = S.DEMO

# Category -> item codes the demo requirements use: (code, text, unit, fallback median rate in ₹).
# The fallback is used only when there are fewer than two real quotes to take a median from.
DEMO_REQS: dict[str, list[tuple[str, str, str, float]]] = {
    "Waterproofing": [
        ("WP.01", "Waterproofing at toilet floor", "sqft", 38), ("WP.02", "Waterproofing at toilet wall", "sqft", 38),
        ("WP.03", "Waterproofing compound on toilet depression", "rmt", 50), ("WP.04", "Waterproofing at kitchen floor", "sqft", 36),
        ("WP.05", "Waterproofing at kitchen wall", "sqft", 36), ("WP.06", "Waterproofing at balcony floor", "sqft", 38),
        ("WP.07", "Waterproofing at balcony wall", "sqft", 38), ("W.P-COBA", "Waterproofing with brick bat coba", "sqft", 95),
        ("STP_UGR", "Nozzle grouting at all construction joints", "rmt", 320),
    ],
    "Cement": [("CEM.01", "OPC 53 grade cement, 50 kg bag", "bag", 410)],
    "Steel": [("TMT.01", "TMT bars Fe 550D", "MT", 61500)],
    "Scaffolding": [("SCF.01", "Cuplock scaffolding on hire, per sqm per month", "sqm", 38)],
    "Tiles & adhesive": [("ADH.01", "Polymer tile adhesive C2TE, 20 kg bag", "bag", 520)],
}
# How many uncategorised vendors to add to each category (on top of the ones already in it).
EXTRA = {"Waterproofing": 3, "Cement": 4, "Steel": 4, "Scaffolding": 4, "Tiles & adhesive": 4}
# Archetypes for the added vendors, in order; chosen so each category tells a story.
STORY = {
    "Waterproofing": ["cheap_unreliable", "major_dispute", "new_thin"],
    "Cement": ["all_rounder", "cheap_unreliable", "premium", "new_thin"],
    "Steel": ["premium", "slow_responder", "major_dispute", "all_rounder"],
    "Scaffolding": ["cheap_unreliable", "all_rounder", "slow_responder", "premium"],
    "Tiles & adhesive": ["major_dispute", "premium", "all_rounder", "new_thin"],
}
# Vendors already in a demo category with real quotes: archetype by price, cheapest first.
BY_PRICE = ["all_rounder", "steady", "slow_responder", "premium"]
OTHER = ["premium", "all_rounder", "steady", "slow_responder", "cheap_unreliable", "major_dispute"]

# price multiple vs median; quality / delivery picked per record from the lists; credit days,
# advance %; RFQ response hours and promised lead days (ranges); records (range); order delay days.
ARCH: dict[str, dict] = {
    "premium":          dict(price=1.16, q=[5, 5, 4], d=[5, 4, 5], credit=(30, 10), resp=(6, 18), lead=(7, 10), recs=(5, 8), late=0),
    "all_rounder":      dict(price=0.97, q=[4, 4, 5, 3], d=[4, 5, 4], credit=(60, 0), resp=(12, 30), lead=(8, 14), recs=(4, 7), late=1),
    "steady":           dict(price=1.00, q=[3, 4, 4], d=[4, 3, 4], credit=(45, 0), resp=(24, 48), lead=(12, 18), recs=(3, 6), late=2),
    "slow_responder":   dict(price=1.02, q=[4, 4, 3], d=[3, 2, 3], credit=(45, 10), resp=(130, 190), lead=(25, 35), recs=(3, 5), late=15),
    "cheap_unreliable": dict(price=0.84, q=[2, 3, 3, 2], d=[2, 3, 1, 2], credit=(0, 50), resp=(30, 60), lead=(20, 30), recs=(4, 6), late=12),
    "major_dispute":    dict(price=0.92, q=[3, 4, 3], d=[3, 3, 2], credit=(30, 20), resp=(40, 70), lead=(14, 20), recs=(3, 5), late=8),
    "new_thin":         dict(price=0.93, q=[], d=[], credit=None, resp=None, lead=None, recs=(0, 0), late=0),
}
PROJECTS = ["Riverside Tower-1", "Riverside Tower-2", "Lakeview Tower-A", "Lakeview Tower-B", "Greenfield Tower-3", "Hillside Phase-2"]
ROLES = ["Site engineer", "Project manager", "QA/QC engineer"]


def rng(vendor_id: str, salt: str) -> random.Random:
    return random.Random(hashlib.sha256(f"{vendor_id}:{salt}".encode()).hexdigest())


def hid(*parts: str) -> str:
    return "demo-" + hashlib.sha256(":".join(parts).encode()).hexdigest()[:10]


def round_rate(r: float) -> float:
    return float(round(r / 50) * 50) if r >= 1000 else float(round(r / 5) * 5) if r >= 100 else round(r * 2) / 2


def paths(args) -> tuple[Path, Path]:
    v = Path(args.vendors or os.environ.get("VENDORS_PATH") or ROOT / "data" / "vendors.json")
    d = Path(args.deals or os.environ.get("VENDOR_DEALS_PATH") or ROOT / "data" / "vendor_deals.json")
    return v, d


# ── strip ──────────────────────────────────────────────────────────────────────────────────────
def strip(vendors: list[dict], deals: list[dict]) -> tuple[list[dict], list[dict]]:
    for v in vendors:
        for k in v.get("demo_fields", []):
            v[k] = None
        dc = {c.lower() for c in v.get("demo_categories", [])}
        if dc:
            v["categories"] = [c for c in v.get("categories", []) if c.lower() not in dc]
            v["categories_to_confirm"] = [c for c in v.get("categories_to_confirm", []) if c.lower() not in dc]
        v["performance"] = [r for r in v.get("performance", []) if r.get("source") != DEMO]
        v["demo_fields"], v["demo_categories"] = [], []
    return vendors, [d for d in deals if d.get("source") != DEMO]


# ── generate ───────────────────────────────────────────────────────────────────────────────────
def perf_records(vid: str, arch: str, work: str) -> list[dict]:
    a, r = ARCH[arch], rng(vid, "perf")
    n = r.randint(*a["recs"])
    major_at = r.randrange(n) if arch == "major_dispute" and n else -1
    out = []
    for i in range(n):
        day = date(2024, 6, 1) + timedelta(days=r.randrange(0, 850))
        disputes = "major" if i == major_at else "minor" if arch == "cheap_unreliable" and r.random() < 0.4 else "none"
        note = {"major": "Leakage after handover; rework and a payment hold.", "minor": "Rework on part of the scope."}.get(disputes, "")
        out.append({"date": day.isoformat(), "project": r.choice(PROJECTS), "work": work, "quality": r.choice(a["q"]),
                    "delivery": r.choice(a["d"]), "disputes": disputes, "note": ("Demo record. " + note).strip(),
                    "recorded_by": r.choice(ROLES), "id": hid(vid, "perf", str(i)), "recorded_at": f"{day.isoformat()}T12:00:00+00:00",
                    "source": DEMO})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def deal_rows(vid: str, arch: str, cat: str, meds: dict[str, dict], real_codes: set[str], price_mult: float) -> list[dict]:
    a, r = ARCH[arch], rng(vid, "deals:" + cat)
    codes = [c for c in DEMO_REQS[cat] if c[0] not in real_codes]
    if not codes:
        return []
    project = r.choice(PROJECTS)
    sent = datetime(2026, 10, 1, 10, 0) + timedelta(days=r.randrange(0, 35))
    hours = r.randint(*a["resp"]) if a["resp"] else None
    lead = r.randint(*a["lead"]) if a["lead"] else None
    out = []
    for code, text, unit, _ in codes:
        rate = round_rate(meds[code]["median"] * price_mult * r.uniform(0.97, 1.03))
        d = D.DealIn(vendor_id=vid, kind="quote", project=project, category=cat, item_code=code, item_text=text, unit=unit, rate=rate,
                     # The thin-data vendor sent a bare rate: no RFQ time, no lead time.
                     date_quoted=(sent + timedelta(hours=hours)).isoformat(timespec="minutes") if hours else sent.date().isoformat(),
                     rfq_sent_at=sent.isoformat(timespec="minutes") if hours else None, lead_time_days_promised=lead,
                     payment_terms="", source=DEMO, notes="Demo quote.")
        out.append(D.Deal(**d.model_dump(), id=hid(vid, cat, code, "q"), response_hours=D.response_hours(d.rfq_sent_at, d.date_quoted)))
    if ARCH[arch]["recs"][1]:  # an older order, so lead time is from an actual delivery
        code, text, unit, _ = codes[0]
        when = date(2026, 3, 1) + timedelta(days=r.randrange(0, 150))
        d = D.DealIn(vendor_id=vid, kind="order", project=r.choice(PROJECTS), category=cat, item_code=code, item_text=text, unit=unit,
                     rate=round_rate(meds[code]["median"] * price_mult * 0.97), date_quoted=when.isoformat(),
                     lead_time_days_promised=lead, lead_time_days_actual=lead + a["late"] + r.randint(0, 2), source=DEMO, notes="Demo order.")
        out.append(D.Deal(**d.model_dump(), id=hid(vid, cat, code, "o"), response_hours=None))
    return [x.model_dump(mode="json") for x in out]


def set_credit(v: dict, arch: str) -> None:
    c = ARCH[arch]["credit"]
    if c is None:
        return
    for k, val in zip(("credit_days", "advance_pct"), c):
        if v.get(k) is None:  # never overwrite a real value
            v[k] = val
            v["demo_fields"].append(k)


def generate(vendors: list[dict], deals: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (vendors, deals, plan) where plan lists who got which archetype."""
    vendors, deals = strip(vendors, deals)
    real = [D.Deal.model_validate(d) for d in deals]
    by_cat = lambda cat: [v for v in vendors if cat.lower() in {c.lower() for c in v.get("categories", [])}]  # noqa: E731
    pool = sorted((v for v in vendors if not v.get("categories") and v.get("status") != "blocked"
                   and not any(d.vendor_id == v["id"] for d in real)), key=lambda v: hashlib.sha256(v["id"].encode()).hexdigest())
    plan: list[dict] = []
    new_deals: list[dict] = []
    done: set[str] = set()
    for cat, items in DEMO_REQS.items():
        codes = [c[0] for c in items]
        cat_real = [d for d in real if d.category.lower() == cat.lower()]
        meds = S.medians(S.latest_rates(cat_real), codes)
        for code, _, unit, fallback in items:
            if code not in meds or meds[code]["unit"] != unit:
                meds[code] = {"unit": unit, "median": fallback}
        existing = by_cat(cat)
        ratios = {}
        for v in existing:
            rs = [d.rate / meds[d.item_code]["median"] for d in cat_real if d.vendor_id == v["id"] and d.item_code in meds and d.unit == meds[d.item_code]["unit"]]
            ratios[v["id"]] = mean(rs) if rs else 1.0
        existing.sort(key=lambda v: (ratios[v["id"]], v["id"]))
        assigned = [(v, BY_PRICE[i % len(BY_PRICE)], ratios[v["id"]]) for i, v in enumerate(existing)]
        for arch in STORY[cat][: EXTRA[cat]]:
            if not pool:
                break
            v = pool.pop(0)
            v["categories"] = [*v.get("categories", []), cat]
            v["demo_categories"].append(cat)
            assigned.append((v, arch, ARCH[arch]["price"]))
        for v, arch, mult in assigned:
            real_codes = {d.item_code for d in cat_real if d.vendor_id == v["id"]}
            new_deals += deal_rows(v["id"], arch, cat, meds, real_codes, mult)
            if v["id"] not in done:
                set_credit(v, arch)
                v["performance"] = sorted(v.get("performance", []) + perf_records(v["id"], arch, f"{cat} work (demo)"),
                                          key=lambda x: x["date"], reverse=True)
                done.add(v["id"])
            plan.append({"vendor_id": v["id"], "category": cat, "archetype": arch, "added_to_category": cat in v["demo_categories"]})
    for v in vendors:  # everyone else with a category: credit terms and history only
        if v["id"] in done or not v.get("categories") or v.get("status") == "blocked":
            continue
        arch = OTHER[int(hashlib.sha256(v["id"].encode()).hexdigest(), 16) % len(OTHER)]
        set_credit(v, arch)
        v["performance"] = sorted(v.get("performance", []) + perf_records(v["id"], arch, f"{v['categories'][0]} work (demo)"),
                                  key=lambda x: x["date"], reverse=True)
        done.add(v["id"])
        plan.append({"vendor_id": v["id"], "category": v["categories"][0], "archetype": arch, "added_to_category": False})
    return vendors, deals + new_deals, plan


# ── files ──────────────────────────────────────────────────────────────────────────────────────
def read(path: Path, key: str) -> dict:
    return json.loads(path.read_text() or "{}") if path.exists() else {"version": 1, key: []}


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dst)
    return dst


def run(vendors_path: Path, deals_path: Path, remove: bool = False, quiet: bool = False) -> list[dict]:
    vdata, ddata = read(vendors_path, "vendors"), read(deals_path, "deals")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for p in (vendors_path, deals_path):
        b = backup(p, stamp)
        if b and not quiet:
            print(f"backup: {b}")
    if remove:
        vendors, deals = strip(vdata.get("vendors", []), ddata.get("deals", []))
        plan: list[dict] = []
    else:
        vendors, deals, plan = generate(vdata.get("vendors", []), ddata.get("deals", []))
    write(vendors_path, vdata | {"vendors": vendors})
    write(deals_path, ddata | {"deals": deals})
    if not quiet:
        if remove:
            print(f"removed all demo data from {vendors_path.name} and {deals_path.name}")
        else:
            n_deals = sum(1 for d in deals if d.get("source") == DEMO)
            n_perf = sum(1 for v in vendors for r in v.get("performance", []) if r.get("source") == DEMO)
            print(f"demo data: {len({p['vendor_id'] for p in plan})} vendors, {n_perf} performance records, {n_deals} deal lines")
            for cat in dict.fromkeys(p["category"] for p in plan):
                rows = [p for p in plan if p["category"] == cat]
                print(f"  {cat}: " + ", ".join(f"{p['archetype']}{' (added)' if p['added_to_category'] else ''}" for p in rows))
    return plan


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--remove", action="store_true", help="strip all demo data")
    ap.add_argument("--vendors", help="vendors.json path")
    ap.add_argument("--deals", help="vendor_deals.json path")
    args = ap.parse_args(argv)
    run(*paths(args), remove=args.remove)


if __name__ == "__main__":
    main()
