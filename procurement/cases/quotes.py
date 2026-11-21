"""Steps 6 and 7: the quotes for a requirement, the comparison, the vendor to award and the email
asking the HOD to approve it. Plain arithmetic and template text, no LLM.

Where a quote comes from, per picked vendor and category:
- uploaded: a PDF a person put on the vendor's row, kept in data/cases/<uid>.quotes/.
- simulated (demo emails only): a quote the demo vendor "sends back" for an RFQ, priced from the
  item code's price median with a fixed spread per vendor (`simulate_quote`), kept in the state.
- the tender folder: a quotation PDF in the data pack (procurement/quotes/pack.py) whose file name
  matches the vendor in the directory (vendors were seeded from those names). It is linked by itself
  when the requirement is the same kind of work as the tender (a shared item code or work word);
  otherwise a person can link it. An upload wins over the tender folder.

The basis: the requirement's approved item codes and quantities, when the quotes price those codes
(an uploaded quote's rows are matched by an item code in the row's text, else in RFQ order). Quotes
from the tender folder that don't price this requirement's codes are compared on that tender's
quantities, and the summary says so. Re-pricing, unit conversion, drift and ranks are
procurement/quotes/compare.py.

State (data/cases/<uid>.json): quote_uploads, pack_links and manual_rates keyed "<vendor>|<category>",
compare (category -> ticked vendor ids), award (category -> {vendor_id, reason, at, …}) and hod (the
email to the HOD, then approved_by_hod_at). Changing an input drops the award and the HOD email after it.
"""

from __future__ import annotations

import hashlib
import os
import re
import smtplib
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..itemcodes import master as M
from ..quotes import compare as C
from ..quotes import pack
from ..quotes import parse as P
from ..quotes.models import Comparison, Quote, QuoteLine, TenderLine, Term
from . import flow as F
from . import reply as R

MAX_PDF = 20 * 1024 * 1024
TERMS = [("gst", "GST"), ("validity_days", "Validity"), ("payment", "Payment"), ("lead_time_days", "Start"), ("warranty", "Warranty")]


class Source(BaseModel):
    vendor_id: str
    vendor: str
    category: str
    source: Literal["upload", "pack", "simulated"]
    file: str
    at: str | None = None
    linked: Literal["auto", "manual"] | None = None  # tender-folder quotes only
    path: str
    data: dict | None = None  # simulated quotes: the Quote itself


def hod_email() -> str:
    return os.environ.get("HOD_EMAIL", "").strip()


# ── Where the quotes are ───────────────────────────────────────────────────────────────────────


def quotes_dir(uid: str) -> Path:
    F._store_path(uid)  # checks the id
    return F.cases_dir() / f"{uid}.quotes"


def upload_path(uid: str, vendor_id: str, category: str, *, stored: bool = False) -> Path:
    """<vendor>.<category slug>.<hash of the exact category>.pdf, so 'Civil & Masonry' and 'Civil Masonry' don't share a file.
    stored: an upload kept before the hash was added (<vendor>.<slug>.pdf) is still read."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", vendor_id):
        raise F.CaseError("Bad vendor id.")
    slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-") or "x"
    p = quotes_dir(uid) / f"{vendor_id}.{slug}.{hashlib.sha1(category.encode()).hexdigest()[:8]}.pdf"
    old = p.with_name(f"{vendor_id}.{slug}.pdf")
    return old if stored and not p.exists() and old.exists() else p


def _vendors() -> list:
    from ..vendors import store as V

    return V.load_all()


def pack_by_vendor(vendors: list) -> dict[str, Path]:
    """Directory vendor id -> its quotation PDF in the tender folder."""
    from ..vendors import seed

    out: dict[str, Path] = {}
    for p in pack.files()["quotes"]:
        v = seed.match(vendors, P.vendor_from_filename(p))
        if v is not None:
            out.setdefault(v.id, p)
    return out


def same_work(lines: list[F.CaseLine], tender: list[TenderLine]) -> bool:
    """The requirement shares an item code or a work word with the tender in the tender folder."""
    if {c for ln in lines for c in ln.codes} & {t.code for t in tender if t.code}:
        return True
    words = set().union(*(M.tokens(t.description) for t in tender)) if tender else set()
    return any(M.tokens(ln.text) & words for ln in lines)


def settled(lines: list[F.CaseLine], category: str) -> list[F.CaseLine]:
    return [ln for ln in lines if ln.category == category and ln.status in ("ready", "decided")]


def sources(uid: str, picks: dict[str, list[str]], lines: list[F.CaseLine], state: dict) -> tuple[list[Source], list[Source]]:
    """The quotes in for the picked vendors, and tender-folder quotes that could be linked but aren't."""
    if not any(picks.values()):
        return [], []
    vendors = _vendors()
    names = {v.id: v.name for v in vendors}
    ups, links = state.get("quote_uploads") or {}, state.get("pack_links") or {}
    by_v = pack_by_vendor(vendors) if pack.files()["quotes"] else {}
    auto: dict[str, bool] = {}
    got, offers = [], []
    for cat, ids in picks.items():
        for vid in ids:
            k, name = F.rfq_key(vid, cat), names.get(vid, vid)
            up = ups.get(k)
            if up and (p := upload_path(uid, vid, cat, stored=True)).exists():
                got.append(Source(vendor_id=vid, vendor=name, category=cat, source="upload", file=up.get("original") or p.name, at=up.get("at"), path=str(p)))
                continue
            if sim := (state.get("sim_quotes") or {}).get(k):
                got.append(Source(vendor_id=vid, vendor=name, category=cat, source="simulated", file=sim["quote"]["file"], at=sim["at"],
                                  path="", data=sim["quote"]))
                continue
            p = by_v.get(vid) if cat == pack.CATEGORY else None
            if p is None:
                continue
            if cat not in auto:
                auto[cat] = same_work(settled(lines, cat), pack.tender())
            s = Source(vendor_id=vid, vendor=name, category=cat, source="pack", file=p.name, linked="manual" if k in links else "auto", path=str(p))
            (got if links.get(k, auto[cat]) else offers).append(s)
    return got, offers


def _parse(path: Path) -> Quote:
    return pack._cached(("case-quote", str(path), path.stat().st_mtime_ns), lambda: P.read_quote_safe(path))


def load_quote(s: Source) -> Quote:
    q = Quote(**s.data) if s.source == "simulated" else _parse(Path(s.path)).model_copy(deep=True)
    q.vendor = s.vendor
    return q


# ── The basis and the comparison ───────────────────────────────────────────────────────────────


def case_basis(lines: list[F.CaseLine], erp_text: dict[str, str]) -> list[TenderLine]:
    """One basis line per approved item code. Its quantity: the lines with only that code, added up (sqm and sqft convert);
    a line with several codes shares one total, so it adds nothing. Units that don't convert: no quantity, the reason in the note."""
    by: dict[str, list[F.CaseLine]] = {}
    for ln in lines:
        for c in ln.codes:
            by.setdefault(c, []).append(ln)
    out: list[TenderLine] = []
    for c, lns in by.items():
        own = [ln for ln in lns if len(ln.codes) == 1]
        units = [P.norm_unit(ln.uom) if ln.uom else "" for ln in own or lns]
        qty, note = None, None
        if own and all(ln.quantity is not None for ln in own):
            factors = [C.convert_rate(1.0, units[0], u) for u in units]  # a quantity in u, in the first line's unit
            if None in factors:
                note = f"the requirement gives it in {', '.join(dict.fromkeys(u.upper() or 'no unit' for u in units))}, which don't add up"
            else:
                qty = sum(ln.quantity * f for ln, f in zip(own, factors))
        out.append(TenderLine(sl_no=len(out) + 1, code=c, description=erp_text.get(c) or lns[0].text, unit=units[0], qty=qty, note=note))
    return out


def _code_in(text: str, codes: list[str]) -> str | None:
    hits = [c for c in codes if re.search(rf"(?<![\w.]){re.escape(c)}(?!\w)", text, re.I)]
    return max(hits, key=len) if hits else None


def line_codes(s: Source, q: Quote, avail: list[str], state: dict, pack_codes: dict[int, str]) -> dict[int, str]:
    """Quote row -> item code. Tender folder: the tender's codes. Upload: a code in the row's text,
    else the RFQ's item order (the vendor numbers its rows as the RFQ did)."""
    if s.source == "pack":
        return {ln.sl_no: pack_codes[ln.sl_no] for ln in q.lines if ln.sl_no in pack_codes}
    chosen = (state.get("rfq_items") or {}).get(F.rfq_key(s.vendor_id, s.category))
    order = [c for c in avail if c in chosen] if chosen else avail
    out: dict[int, str] = {}
    for ln in q.lines:  # a code goes to the first row that names it
        if (c := _code_in(ln.description, avail)) and c not in out.values():
            out[ln.sl_no] = c
    for ln in q.lines:
        if ln.sl_no in out:
            continue
        free = [c for c in order if c not in out.values()]
        pos = order[ln.sl_no - 1] if 0 < ln.sl_no <= len(order) else None
        if c := pos if pos in free else (free[0] if free else None):
            out[ln.sl_no] = c
    return out


def rate_key(t: TenderLine) -> str:
    return t.code or f"#{t.sl_no}"


def apply_rates(q: Quote, basis: list[TenderLine], rates: dict[str, float | None], at: str | None) -> None:
    """Rates a person typed in from the PDF (a scan, say), per basis line."""
    if not rates:
        return
    mine = {ln.sl_no: ln for ln in q.lines}
    n = 0
    for t in basis:
        r = rates.get(rate_key(t))
        if r is None:
            continue
        n += 1
        if t.sl_no in mine:
            ln = mine[t.sl_no]
            ln.rate, ln.amount, ln.arithmetic_ok = float(r), None, None
        else:
            q.lines.append(QuoteLine(sl_no=t.sl_no, code=t.code, description=t.description, unit=t.unit, unit_as_written=t.unit.upper(),
                                     qty=None, rate=float(r), amount=None, page=0))
    q.lines.sort(key=lambda ln: ln.sl_no)
    if n and all(ln.rate is not None for ln in q.lines if ln.sl_no in {t.sl_no for t in basis}):
        q.needs_vision, q.read_confidence = False, "medium"
    q.notes.append(f"{n} rate{'s' * (n != 1)} typed in by a person from the PDF" + (f" ({at[:10]})." if at else "."))


def _who(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _term(key: str, v: str | None) -> str:
    if not v:
        return "not stated"
    if key == "validity_days":
        return f"{v} days"
    if key == "lead_time_days":
        return f"within {v} days"
    if key == "payment":
        v = re.sub(r"^\s*(?:\d{1,2}|[a-z])\s*[.)]?\s+(?=\S)", "", v)
    return v if len(v) <= 70 else v[:67].rstrip() + "…"


def summary(kind: str, basis: list[TenderLine], avail: list[str], cmp: Comparison | None, chosen: list[Quote],
            loaded: list[Quote], left_out: list[str]) -> list[str]:
    """Plain-English points, built only from the data."""
    out = []
    if kind == "tender":
        out.append(f"These quotes were priced on the tender BOQ in the tender folder, not on this requirement's item code"
                   f"{'s' * (len(avail) != 1)} ({', '.join(avail)}), so they are compared on that tender's quantities ({len(basis)} lines).")
    else:
        out.append(f"Compared on this requirement's approved quantities ({len(basis)} item code{'s' * (len(basis) != 1)}).")
        noqty = [t.code for t in basis if t.qty is None and not t.note]
        if noqty:
            out.append(f"No quantity in the requirement for {', '.join(noqty)}: rates are shown, but "
                       f"{'it is' if len(noqty) == 1 else 'they are'} left out of the like-for-like totals.")
        for t in basis:
            if t.qty is None and t.note:
                out.append(f"No single quantity for {t.code}: {t.note}. Its rates are shown, but it is left out of the like-for-like totals.")
    for q in loaded:
        if q.needs_vision:
            out.append(f"{q.vendor}'s quote is a scan without readable rates: it needs a person to read the rates "
                       "(type them in under the quote) before it can be compared.")
    if left_out:
        out.append(f"Left out of the comparison: {_who(left_out)}.")
    if cmp is None:
        return out
    vs = cmp.vendors
    ranked = sorted((v for v in vs if v.rank_repriced), key=lambda v: v.rank_repriced)
    if ranked:
        low = ranked[0]
        rest = "; ".join(f"{v.vendor}" + (f" {v.pct_above_lowest:+.1f}%" if v.pct_above_lowest is not None else "") + f" ({C.inr(v.repriced_total)})"
                         for v in ranked[1:])
        out.append(f"Lowest on the same quantities: {low.vendor} at {C.inr(low.repriced_total)} excl. GST." + (f" Then {rest}." if rest else ""))
    else:
        out.append("No quote can be ranked like for like: there is no complete quote with a quantity to price it on.")
    cq = next((v for v in vs if v.rank_as_quoted == 1), None)
    other = [v.vendor for v in vs if v.quantity_basis == "different"]
    if ranked and cq is not None and cq.rank_repriced != 1 and len(vs) > 1:
        if cq.rank_repriced is None:
            out.append(f"Trap: {cq.vendor} looks cheapest as quoted ({C.inr(cq.as_quoted_total)}), but it doesn't price every line, "
                       "so that total isn't comparable.")
        else:
            why = f" {_who(other)} priced a different quantity set, so {'their PDF totals aren' if len(other) > 1 else 'its PDF total isn'}'t comparable." if other else ""
            out.append(f"Trap: {cq.vendor} looks cheapest as quoted ({C.inr(cq.as_quoted_total)}, its own PDF total), but on the same "
                       f"quantities it is {f'{cq.pct_above_lowest:.0f}% above' if cq.pct_above_lowest is not None else 'not'} the lowest.{why}")
    elif other:
        out.append(f"{_who(other)} priced a different quantity set: {'their' if len(other) > 1 else 'its'} PDF total isn't comparable; the re-priced figure is.")
    for n in cmp.non_comparable:
        out.append(f"{n.code or 'Line ' + str(n.sl_no)} ({n.description.lower()}) is {n.reason[0].lower()}{n.reason[1:-1]}. "
                   "It is left out of the totals: ask for a measured quantity.")
    for v in vs:
        if not v.complete and not v.needs_vision:
            measured = {t.sl_no for t in cmp.tender if t.measured}
            gaps = [ln.code or str(ln.sl_no) for ln in v.lines if ln.sl_no in measured
                    and any(f in ("Not quoted.", "No rate read.") or "don't convert" in f for f in ln.flags)]
            out.append(f"{v.vendor}'s quote is incomplete (no comparable rate for {', '.join(gaps) or 'some lines'}), so it isn't ranked on the same quantities.")
        elif v.read_confidence != "high" and not v.needs_vision:
            q = next(q for q in chosen if q.vendor == v.vendor)
            out.append(f"{v.vendor} was read with {v.read_confidence} confidence" + (f": {q.notes[0][0].lower()}{q.notes[0][1:]}" if q.notes else ".") + " Check its rates against the PDF.")
    for key, label in TERMS:
        vals = {q.vendor: q.term(key) for q in chosen}
        if len(chosen) > 1 and len(set(vals.values())) > 1:
            groups: dict[str, list[str]] = {}
            for name, val in vals.items():
                groups.setdefault(_term(key, val), []).append(name)
            out.append(f"{label} differs: " + "; ".join(f"{t} ({', '.join(ns)})" for t, ns in groups.items()) + ".")
    return out


def recommend(cmp: Comparison | None, ids: list[str]) -> dict | None:
    """The lowest complete quote on the same quantities; failing that, the lowest complete one as quoted."""
    if cmp is None:
        return None
    pairs = list(zip(cmp.vendors, ids))
    ranked = sorted(((v, i) for v, i in pairs if v.rank_repriced), key=lambda x: x[0].rank_repriced)
    if ranked:
        v, vid = ranked[0]
        n2 = ranked[1][0] if len(ranked) > 1 else None
        nxt = "" if n2 is None else f" The next, {n2.vendor}, is " + (f"{n2.pct_above_lowest:.1f}% higher." if n2.pct_above_lowest is not None
                                                                     else f"at {C.inr(n2.repriced_total)}.")
        return {"vendor_id": vid, "vendor": v.vendor, "like_for_like": True, "margin_pct": n2.pct_above_lowest if n2 else None,
                "reason": f"Lowest complete quote on the same quantities: {C.inr(v.repriced_total)} excl. GST.{nxt}"}
    # No quantities to re-price on, but every quote gives a rate per unit on every line: a vendor lowest on
    # every line is the clear choice (rates per unit compare like for like whatever the quantities).
    lines = [t.sl_no for t in cmp.tender if t.measured]
    no_qty = all(t.qty is None for t in cmp.tender if t.measured)  # only when there really is nothing to total
    rated = [(v, i, {ln.sl_no: ln.rate_per_tender_unit for ln in v.lines}) for v, i in pairs if not v.needs_vision and v.complete]
    if lines and no_qty and len(rated) == len(pairs) and all(r.get(n) is not None for _, _, r in rated for n in lines):
        best = [(v, i) for v, i, r in rated if all(r[n] <= min(o[n] for _, _, o in rated) + 0.005 for n in lines)]
        if len(best) == 1:
            v, vid = best[0]
            unit = {t.sl_no: t.unit for t in cmp.tender}
            mine = {ln.sl_no: ln.rate_per_tender_unit for ln in v.lines}
            gaps = [(min(r[n] for w, _, r in rated if w is not v) - mine[n]) / mine[n] * 100 for n in lines if len(rated) > 1 and mine[n]]
            if len(lines) == 1:
                others = sorted(r[lines[0]] for w, _, r in rated if w is not v)
                nxt = f" The next is ₹{others[0]:,.2f}." if others else ""
                why = f"Lowest rate: ₹{mine[lines[0]]:,.2f}/{unit[lines[0]] or 'unit'} excl. GST.{nxt}"
            else:
                why = f"Lowest rate on all {len(lines)} lines."
            return {"vendor_id": vid, "vendor": v.vendor, "like_for_like": True, "by_rate": True,
                    "margin_pct": round(min(gaps), 2) if gaps else None,
                    "reason": why + " The requirement gives no quantities to total, so this is on rates per unit."}
    quoted = sorted(((v, i) for v, i in pairs if v.complete and not v.needs_vision and v.as_quoted_total is not None), key=lambda x: x[0].as_quoted_total)
    if quoted:
        v, vid = quoted[0]
        return {"vendor_id": vid, "vendor": v.vendor, "like_for_like": False,
                "reason": f"Lowest as quoted ({C.inr(v.as_quoted_total)} excl. GST). There are no common quantities to compare like for like, "
                          "so check the quantities before awarding."}
    return None


def _lows(cmp: Comparison, ids: list[str]) -> dict[int, list[str]]:
    """Per basis line, the vendor(s) lowest: on the re-priced amount, or on the rate when there's no quantity."""
    out = {}
    for t in cmp.tender:
        cells = [(i, next(ln for ln in v.lines if ln.sl_no == t.sl_no)) for v, i in zip(cmp.vendors, ids)]
        vals = [(i, ln.repriced) for i, ln in cells if ln.comparable and ln.repriced is not None]
        if not vals:
            vals = [(i, ln.rate_per_tender_unit) for i, ln in cells if ln.rate_per_tender_unit is not None and t.measured]
        if len(vals) > 1:
            m = min(x for _, x in vals)
            out[t.sl_no] = [i for i, x in vals if abs(x - m) < 0.005]
    return out


def category_view(case: F.Case, cat: str, lines: list[F.CaseLine], srcs: list[Source], offers: list[Source], state: dict,
                  erp_text: dict[str, str]) -> dict:
    avail = F.category_codes(lines)
    loaded = [(s, load_quote(s)) for s in srcs]
    ptender = pack.tender() if any(s.source == "pack" for s in srcs) else []
    maps = [line_codes(s, q, avail, state, {t.sl_no: t.code for t in ptender if t.code}) for s, q in loaded]
    priced = {c for m in maps for c in m.values()}
    if ptender and not priced & set(avail):
        kind, basis = "tender", ptender
    else:
        kind, basis = "requirement", case_basis(lines, erp_text)
        idx = {t.code: t.sl_no for t in basis}
        for (_, q), m in zip(loaded, maps):
            for ln in q.lines:
                ln.sl_no = idx.get(m.get(ln.sl_no), 1000 + ln.sl_no)
            q.lines.sort(key=lambda ln: ln.sl_no)
    for t in basis:
        if not t.unit:
            units = Counter(ln.unit for _, q in loaded for ln in q.lines if ln.sl_no == t.sl_no)
            t.unit = units.most_common(1)[0][0] if units else ""
    manual = state.get("manual_rates") or {}
    for s, q in loaded:
        m = manual.get(F.rfq_key(s.vendor_id, cat)) or {}
        apply_rates(q, basis, m.get("rates") or {}, m.get("at"))
    saved = (state.get("compare") or {}).get(cat)
    ids = [s.vendor_id for s, _ in loaded]
    selected = [v for v in saved if v in ids] if saved is not None else [s.vendor_id for s, q in loaded if not q.needs_vision]
    chosen = [(s, q) for s, q in loaded if s.vendor_id in selected]
    cmp = C.compare(basis, [q for _, q in chosen]) if chosen else None  # basis lines aren't changed by it
    sel_ids = [s.vendor_id for s, _ in chosen]
    rec = recommend(cmp, sel_ids)
    award = (state.get("award") or {}).get(cat)
    left_out = [s.vendor for s, q in loaded if s.vendor_id not in selected and not q.needs_vision]
    return {
        "category": cat,
        "item_codes": avail,
        "basis": {"kind": kind, "lines": [t.model_dump() for t in basis]},
        "quotes": [s.model_dump(exclude={"path", "data"}) | {"quote": q.model_dump(), "selected": s.vendor_id in selected,
                                                     "manual_rates": (manual.get(F.rfq_key(s.vendor_id, cat)) or {}).get("rates") or {}}
                   for s, q in loaded],
        "offers": [s.model_dump(exclude={"path", "data"}) for s in offers if s.category == cat],
        "waiting": [{"vendor_id": vid, "vendor": n} for vid, n in _names(case.vendor_picks.get(cat, [])) if vid not in ids],
        "selected": sel_ids,
        "comparison": cmp.model_dump() | {"vendor_ids": sel_ids, "lowest": _lows(cmp, sel_ids)} if cmp else None,
        "summary": summary(kind, basis, avail, cmp, [q for _, q in chosen], [q for _, q in loaded], left_out),
        "recommended": rec,
        "award": award,
    }


def _names(ids: list[str]) -> list[tuple[str, str]]:
    names = {v.id: v.name for v in _vendors()}
    return [(i, names.get(i, i)) for i in ids]


def view(case: F.Case) -> dict:
    """Steps 5 and 6 for a case: per category the quotes, the comparison, the summary and the award;
    then the HOD email draft once every category has a vendor chosen."""
    if not case.approved_at or not case.vendor_picks:
        return {"open": False, "categories": [], "hod_draft": None}
    state = F.load_state(case.uid)
    srcs, offers = sources(case.uid, case.vendor_picks, case.lines, state)
    groups, _ = F.approved_groups(case)
    by_code = M.get_master().by_code()
    erp_text = {c: es[0].title for c, es in by_code.items()}
    cats = [category_view(case, cat, groups.get(cat, []), [s for s in srcs if s.category == cat], offers, state, erp_text)
            for cat, ids in case.vendor_picks.items() if ids and groups.get(cat)]
    return {"open": bool(case.rfqs or srcs), "categories": cats, "hod_draft": hod_draft(case, cats),
            "hod_email": hod_email()}


def _cat(case: F.Case, category: str) -> dict:
    cv = next((c for c in view(case)["categories"] if c["category"] == category), None)
    if cv is None:
        raise F.CaseError(f"No picked vendors for {category}.")
    return cv


# ── Changes a person makes ─────────────────────────────────────────────────────────────────────


def _picked(case: F.Case, vendor_id: str, category: str) -> None:
    if not case.approved_at:
        raise F.CaseError("Approve the items first.")
    if vendor_id not in case.vendor_picks.get(category, []):
        raise F.CaseError(f"Vendor {vendor_id} isn't picked for {category}.")


def drop_award(state: dict, category: str | None = None) -> None:
    """The comparison changed: the vendor chosen on it, and the HOD email about it, no longer stand."""
    aw = state.get("award") or {}
    if category is None:
        aw.clear()
    else:
        aw.pop(category, None)
    if aw:
        state["award"] = aw
    else:
        state.pop("award", None)
    if state.get("hod"):
        state.setdefault("hod_history", []).append(state.pop("hod"))
    state.pop("hod", None)


def upload(uid: str, vendor_id: str, category: str, data: bytes, filename: str | None) -> F.Case:
    case = F.build_case(uid)
    _picked(case, vendor_id, category)
    if not data.startswith(b"%PDF"):
        raise F.CaseError("That isn't a PDF.")
    if len(data) > MAX_PDF:
        raise F.CaseError("That PDF is over 20 MB.")
    p = upload_path(uid, vendor_id, category)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    state = F.load_state(uid)
    k = F.rfq_key(vendor_id, category)
    state.setdefault("quote_uploads", {})[k] = {"file": p.name, "original": Path(filename or p.name).name, "at": F.now()}
    (state.get("manual_rates") or {}).pop(k, None)  # typed for the old file
    if (saved := (state.get("compare") or {}).get(category)) is not None and vendor_id not in saved:
        saved.append(vendor_id)
    drop_award(state, category)
    F.log_event(state, "quote", f"Quote uploaded for {vendor_id} ({category}): {Path(filename or p.name).name}.", vendor_id=vendor_id, category=category)
    F.save_state(uid, state)
    return F.build_case(uid)


def link(uid: str, vendor_id: str, category: str, use: bool) -> F.Case:
    """Use (or stop using) the vendor's quote from the tender folder."""
    case = F.build_case(uid)
    _picked(case, vendor_id, category)
    state = F.load_state(uid)
    srcs, offers = sources(uid, case.vendor_picks, case.lines, state)
    if not any(s.vendor_id == vendor_id and s.category == category and s.source == "pack" for s in srcs + offers):
        raise F.CaseError("There's no quote from this vendor in the tender folder.")
    state.setdefault("pack_links", {})[F.rfq_key(vendor_id, category)] = use
    drop_award(state, category)
    F.log_event(state, "quote", f"{'Used' if use else 'Stopped using'} the tender-folder quote from {vendor_id} ({category}).", vendor_id=vendor_id)
    F.save_state(uid, state)
    return F.build_case(uid)


def set_rates(uid: str, vendor_id: str, category: str, rates: dict[str, float | None]) -> F.Case:
    case = F.build_case(uid)
    _picked(case, vendor_id, category)
    cv = _cat(case, category)
    if not any(q["vendor_id"] == vendor_id for q in cv["quotes"]):
        raise F.CaseError("No quote from this vendor yet.")
    keys = {rate_key(TenderLine(**t)) for t in cv["basis"]["lines"]}
    bad = [k for k in rates if k not in keys]
    if bad:
        raise F.CaseError(f"Not a line in this comparison: {', '.join(bad)}.")
    if any(r is not None and r <= 0 for r in rates.values()):
        raise F.CaseError("A rate must be more than 0.")
    clean = {k: r for k, r in rates.items() if r is not None}
    state = F.load_state(uid)
    mr = state.setdefault("manual_rates", {})
    k = F.rfq_key(vendor_id, category)
    if clean:
        mr[k] = {"rates": clean, "at": F.now()}
    else:
        mr.pop(k, None)
    if clean and (saved := (state.get("compare") or {}).get(category)) is not None and vendor_id not in saved:
        saved.append(vendor_id)
    drop_award(state, category)
    F.log_event(state, "quote", f"Rates typed in for {vendor_id} ({category}): {len(clean)}.", vendor_id=vendor_id)
    F.save_state(uid, state)
    return F.build_case(uid)


def pick_compare(uid: str, category: str, vendor_ids: list[str]) -> F.Case:
    case = F.build_case(uid)
    cv = _cat(case, category)
    have = {q["vendor_id"] for q in cv["quotes"]}
    bad = [v for v in vendor_ids if v not in have]
    if bad:
        raise F.CaseError(f"No quote in from: {', '.join(bad)}.")
    if not vendor_ids:
        raise F.CaseError("Tick at least one quote to compare.")
    state = F.load_state(uid)
    state.setdefault("compare", {})[category] = list(dict.fromkeys(vendor_ids))
    if set(vendor_ids) != set(cv["selected"]):  # a different comparison: the award and HOD email on the old one go
        drop_award(state, category)
        F.log_event(state, "compare", f"Comparing {len(vendor_ids)} quote{'s' * (len(vendor_ids) != 1)} for {category}.")
    F.save_state(uid, state)
    return F.build_case(uid)


def award(uid: str, category: str, vendor_id: str | None, reason: str | None = None) -> F.Case:
    """Choose the vendor for a category: the recommended one, or another with a reason. None clears it."""
    case = F.build_case(uid)
    cv = _cat(case, category)
    state = F.load_state(uid)
    if vendor_id is None:
        drop_award(state, category)
        F.log_event(state, "award", f"Cleared the vendor chosen for {category}.")
        F.save_state(uid, state)
        return F.build_case(uid)
    if vendor_id not in cv["selected"]:
        raise F.CaseError("Tick that vendor's quote in the comparison first.")
    if {(c, v) for c, ids in case.vendor_picks.items() for v in ids} - {(r.category, r.vendor_id) for r in case.rfqs}:
        raise F.CaseError("Send or mark as sent every RFQ first.")
    rec = cv["recommended"]
    override = rec is None or rec["vendor_id"] != vendor_id
    reason = (reason or "").strip()
    if override and not reason:
        raise F.CaseError("Say why you're choosing a vendor other than the recommended one.")
    name = next(q["vendor"] for q in cv["quotes"] if q["vendor_id"] == vendor_id)
    drop_award(state, category)
    state.setdefault("award", {})[category] = {"vendor_id": vendor_id, "vendor": name, "reason": reason or rec["reason"], "at": F.now(),
                                               "recommended": rec["vendor_id"] if rec else None, "override": override}
    F.log_event(state, "award", f"Chose {name} for {category}" + (" (not the recommended vendor)" if override else "") + f": {reason or rec['reason']}",
                vendor_id=vendor_id, category=category)
    F.save_state(uid, state)
    return F.build_case(uid)


# ── Demo: a vendor's quote, simulated ──────────────────────────────────────────────────────────

# Rupees per unit when the vendor history has no median for a code (demo only).
BASE_RATE = {"Waterproofing": 420.0, "Cement": 385.0, "Steel": 62.0, "Scaffolding": 48.0, "Tiles & adhesive": 510.0, "Concrete": 5200.0}


def _spread(*parts: str, lo: float, hi: float) -> float:
    """A fixed number in [lo, hi] for these strings, so a vendor's simulated rates are the same every run."""
    h = int(hashlib.sha1("|".join(parts).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return lo + (hi - lo) * h


def simulate_quote(uid: str, vendor_id: str, category: str) -> F.Case:
    """Demo emails only: the vendor's quote for the RFQ we sent comes back. One row per item code on the
    RFQ, on the requirement's quantities; rate = the code's price median (or a base rate) x a fixed
    spread per vendor and code (about -10% to +18%), so vendors differ but the result is repeatable."""
    from ..shortlist import score as S
    from ..vendors import store as V

    if not uid.isdigit():
        raise F.CaseError("Simulated quotes are for demo emails only.")
    case = F.build_case(uid)
    _picked(case, vendor_id, category)
    rfq = next((r for r in case.rfqs if (r.vendor_id, r.category) == (vendor_id, category)), None)
    if rfq is None:
        raise F.CaseError("Send (or mark as sent) the RFQ to this vendor first.")
    groups, _ = F.approved_groups(case)
    lines = groups.get(category, [])
    state = F.load_state(uid)
    avail = F.category_codes(lines)
    chosen = (state.get("rfq_items") or {}).get(F.rfq_key(vendor_id, category))
    codes = [c for c in avail if c in chosen] if chosen else avail
    erp = {c: es[0].title for c, es in M.get_master().by_code().items()}
    basis = {t.code: t for t in case_basis(lines, erp)}
    meds = S.shortlist(category, codes).medians
    v = next((x for x in V.load_all() if x.id == vendor_id), None)
    name = v.name if v else vendor_id
    bias = _spread(vendor_id, lo=0.94, hi=1.10)
    qlines = []
    for i, code in enumerate(codes, start=1):
        t = basis.get(code)
        med = meds.get(code) or {}
        unit = (t.unit if t and t.unit else "") or med.get("unit") or "nos"
        rate = round((med.get("median") or BASE_RATE.get(category, 100.0)) * bias * _spread(vendor_id, code, lo=0.95, hi=1.08), 2)
        qty = t.qty if t else None
        qlines.append(QuoteLine(sl_no=i, code=code, description=f"{code} {erp.get(code) or (t.description if t else code)}", unit=unit,
                                unit_as_written=unit.upper(), qty=qty, rate=rate, amount=round(rate * qty, 2) if qty else None, page=1,
                                arithmetic_ok=True if qty else None))
    lead = int(_spread(vendor_id, "lead", lo=5, hi=15))
    pay = V.credit_label(v.credit_days, v.advance_pct) if v else "30 days credit"
    terms = [Term(key="gst", value="18% extra", page=1, text="GST 18% extra"),
             Term(key="validity_days", value="30", page=1, text="Valid for 30 days"),
             Term(key="payment", value=pay if pay != "not known" else "30 days credit", page=1, text=f"Payment: {pay}"),
             Term(key="lead_time_days", value=str(lead), page=1, text=f"Mobilise within {lead} days")]
    q = Quote(vendor=name, file=f"{vendor_id}-quote-simulated.pdf", date=F.now()[:10], lines=qlines, terms=terms, read_confidence="high",
              notes=["Simulated for the demo: not a real quote."])
    state.setdefault("sim_quotes", {})[F.rfq_key(vendor_id, category)] = {"at": F.now(), "quote": q.model_dump()}
    if (saved := (state.get("compare") or {}).get(category)) is not None and vendor_id not in saved:
        saved.append(vendor_id)  # like an upload: a quote that comes in joins a comparison already picked
    total = sum(ln.amount or 0 for ln in qlines)
    F.log_event(state, "quote", f"{name} quoted for {category}" + (f": {C.inr(total)} excl. GST" if total else "") + " (simulated).",
                actor="vendor", vendor_id=vendor_id, category=category)
    drop_award(state, category)
    F.save_state(uid, state)
    return F.build_case(uid)


# ── Step 6: the email to the HOD ───────────────────────────────────────────────────────────────


def _table(cv: dict) -> list[str]:
    cmp = cv["comparison"]
    if not cmp:
        return ["(no quotes compared)"]
    quotes = {q["vendor_id"]: q["quote"] for q in cv["quotes"]}
    w = max([len("Vendor")] + [min(len(v["vendor"]), 30) for v in cmp["vendors"]])
    rows = [f"{'Vendor'.ljust(w)}  {'Like for like':>14}  {'Rank':>4}  {'As quoted':>12}  Terms"]
    order = sorted(zip(cmp["vendors"], cmp["vendor_ids"]), key=lambda x: (x[0]["rank_repriced"] is None, x[0]["rank_repriced"] or 0))
    for v, vid in order:
        q = Quote(**quotes[vid])
        terms = "; ".join(f"{label} {_term(k, q.term(k))}" for k, label in TERMS if k != "payment" and q.term(k)) or "terms not stated"
        rank = str(v["rank_repriced"]) if v["rank_repriced"] else "–"
        lfl = C.inr(v["repriced_total"]) + ("" if v["complete"] else "*")
        rows.append(f"{v['vendor'][:30].ljust(w)}  {lfl:>14}  {rank:>4}  {C.inr(v['as_quoted_total']):>12}  {terms}")
    if any(not v["complete"] for v in cmp["vendors"]):
        rows.append("* incomplete: not every line priced, so not ranked")
    return rows


def hod_draft(case: F.Case, cats: list[dict]) -> dict | None:
    if not cats or not all(c["award"] for c in cats):
        return None
    facts = _facts(case.uid)
    where = ", ".join(x for x in (facts["block"], facts["project"]) if x) or "our project"
    work = facts["work"] or ", ".join(c["category"] for c in cats)
    names = [c["award"]["vendor"] for c in cats]
    subject = f"For approval: {_who(names)} for {work.lower()} at {where}"
    body = ["Dear Sir,", "",
            f"Please approve the vendor below for {work.lower()} at {where}.", "",
            f"Requirement: {case.subject or 'requirement email'}" + (f" (from {case.sender}, {case.date})" if case.sender else "") + "."]
    if facts["start"]:
        body.append(f"Needed by / start: {facts['start']}.")
    for c in cats:
        aw, cmp = c["award"], c["comparison"]
        n_q, n_p = len(c["quotes"]), len(case.vendor_picks.get(c["category"], []))
        basis = ("on the tender BOQ quantities (the quotes were priced on it)" if c["basis"]["kind"] == "tender"
                 else "on this requirement's quantities")
        body += ["", f"{c['category']}: RFQs to {n_p} vendor{'s' * (n_p != 1)}, {n_q} quote{'s' * (n_q != 1)} in, "
                     f"{len(c['selected'])} compared {basis}. Rates excl. GST.", "", *_table(c), ""]
        body.append(f"Recommended: {aw['vendor']}. {aw['reason']}")
        if aw.get("override") and c["recommended"]:
            body.append(f"(Lowest on the comparison was {c['recommended']['vendor']}: {c['recommended']['reason']})")
        caveats = [s for s in c["summary"][1:] if not s.startswith("Lowest on the same quantities")]
        if caveats:
            body += ["", "Points to note:"] + [f"- {s}" for s in caveats]
    body += ["", "Please reply to approve, or with any changes (another vendor, a negotiation round, or questions for the vendors).", "",
             "Thanks & Regards,", "Contracts & Planning"]
    return {"to": hod_email(), "subject": subject, "body": "\n".join(body), "sent": F.load_state(case.uid).get("hod")}


def _facts(uid: str) -> dict:
    from . import rfq as Q

    return Q.requirement_facts(uid)


def send_hod(uid: str, to: str, subject: str, body: str, *, mark_only: bool = False, smtp_factory=smtplib.SMTP_SSL) -> dict:
    case = F.build_case(uid)
    if case.stage not in ("hod", "done"):
        raise R.SendError("Choose the vendor in step 5 first.", 400)
    addr, via = R.deliver(to, subject, body, mark_only=mark_only, smtp_factory=smtp_factory)
    state = F.load_state(uid)
    if state.get("hod"):
        state.setdefault("hod_history", []).append(state["hod"])
    state["hod"] = {"to": addr, "subject": subject, "body": body, "at": F.now(), "via": via}
    F.log_event(state, "email_hod", f"Email to the HOD {'sent' if via == 'smtp' else 'marked as sent'} to {addr}.")
    F.save_state(uid, state)
    return state["hod"]


def approve_hod(uid: str, approved: bool = True) -> F.Case:
    case = F.build_case(uid)
    state = F.load_state(uid)
    hod = state.get("hod")
    if case.stage not in ("hod", "done") or not hod:
        raise F.CaseError("Send the email to the HOD (or mark it as sent) first.")
    if approved:
        hod["approved_by_hod_at"] = F.now()
    else:
        hod.pop("approved_by_hod_at", None)
    F.log_event(state, "hod", "HOD approved." if approved else "HOD approval undone.")
    F.save_state(uid, state)
    return F.build_case(uid)
