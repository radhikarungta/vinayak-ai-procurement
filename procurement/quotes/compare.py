"""Re-price every quote on the same tender quantities. Plain arithmetic, no LLM.

Why: vendors price on whatever quantities they were sent. If one quoted on an older, larger set,
adding up each PDF's own total ranks them on quantity, not on price. So each vendor's rate is
multiplied by the tender quantity for that line, and only lines that every vendor can be compared
on are added up.

A line is left out of the comparable total (and listed with the reason) when:
- the site sheet says it wasn't measured: the tender figure is a placeholder, not a quantity; or
- the vendor's unit can't be converted to the tender's (per sqm vs per running metre).
sqm and sqft convert (1 sqm = 10.7639 sqft), with a note.

Quantity drift: a vendor quantity within 0.05% of the tender is the same; up to 2% is rounding
(sqm to sqft); more than that means the vendor priced a different quantity set.
"""

from __future__ import annotations

from ..intake.checks import SQFT_PER_SQM
from ..intake.models import QuantitySheet
from .models import Comparison, LineCompare, NonComparable, Quote, QuoteLine, TenderLine, VendorCompare

SAME = 0.0005
ROUNDING = 0.02


def inr(x: float | None) -> str:
    """Rupees with Indian grouping: 1875112.3 -> ₹18,75,112."""
    if x is None:
        return "–"
    n = round(x)
    s, neg = str(abs(n)), n < 0
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-₹" if neg else "₹") + s


def _qty(x: float | None) -> str:
    return "–" if x is None else f"{x:,.2f}".rstrip("0").rstrip(".")


def convert_rate(rate: float, from_unit: str, to_unit: str) -> float | None:
    """Rate per `from_unit` as a rate per `to_unit`, or None if they don't convert."""
    if from_unit == to_unit:
        return rate
    if (from_unit, to_unit) == ("sqm", "sqft"):
        return rate / SQFT_PER_SQM
    if (from_unit, to_unit) == ("sqft", "sqm"):
        return rate * SQFT_PER_SQM
    return None


# ── Tender basis ───────────────────────────────────────────────────────────────────────────────
def build_tender(tender_lines: list[QuoteLine] | None, sheet: QuantitySheet | None) -> list[TenderLine]:
    """Tender quantities from the tender BOQ (rates blank), item codes and 'measured' from the site sheet.
    Without the tender BOQ, quantities come from the site sheet (sqm converted to sqft)."""
    by_sl = {}
    if sheet is not None:
        for b in sheet.lines:
            try:
                by_sl[int(str(b.sl_no).strip())] = b
            except (TypeError, ValueError):
                continue
    out: list[TenderLine] = []
    if tender_lines:
        for t in tender_lines:
            b = by_sl.get(t.sl_no)
            sheet_sqft = b.total_sqm * SQFT_PER_SQM if b is not None and b.total_sqm is not None else None
            out.append(TenderLine(sl_no=t.sl_no, code=b.code if b else None, description=t.description, unit=t.unit, qty=t.qty,
                                  measured=b.measured if b else True, sheet_sqft=sheet_sqft, sheet_sqft_written=b.total_sqft if b else None,
                                  note=(b.remarks if b is not None and not b.measured else None)))
    else:
        for sl, b in sorted(by_sl.items()):
            sqft = b.total_sqm * SQFT_PER_SQM if b.total_sqm is not None else None
            area = (b.uom or "").upper().startswith("SQ")
            out.append(TenderLine(sl_no=sl, code=b.code, description=b.description, unit="sqft" if area else (b.uom or "").lower(),
                                  qty=sqft if area else None, measured=b.measured, sheet_sqft=sqft, sheet_sqft_written=b.total_sqft, note=b.remarks if not b.measured else None))
    return out


def _largest(pairs: list[tuple[TenderLine, float, float]]) -> tuple[TenderLine, float, float] | None:
    return max(pairs, key=lambda x: abs(x[1] / x[2] - 1)) if pairs else None


def tender_notes(tender: list[TenderLine]) -> list[str]:
    notes = []
    area = [t for t in tender if t.qty and t.unit == "sqft"]
    if big := _largest([(t, t.qty, t.sheet_sqft) for t in area if t.sheet_sqft]):
        t, a, b = big
        d = a / b - 1
        if abs(d) > ROUNDING:
            notes.append(f"Tender quantity for {t.code or 'line ' + str(t.sl_no)} is {_qty(a)} sqft but the site sheet gives {_qty(b)} "
                         f"(sqm x {SQFT_PER_SQM}), {d * 100:+.1f}%. Check which one is current.")
        else:
            notes.append(f"Tender quantities are the site sheet's sqm x {SQFT_PER_SQM} to within {abs(d) * 100:.3f}%.")
    if big := _largest([(t, t.qty, t.sheet_sqft_written) for t in area if t.sheet_sqft_written]):
        t, a, b = big
        d = a / b - 1
        if SAME * 0.1 < abs(d) <= ROUNDING:
            factor = next((x.sheet_sqft_written / (x.sheet_sqft / SQFT_PER_SQM) for x in area if x.sheet_sqft and x.sheet_sqft_written), None)
            notes.append(f"The site sheet's own sqft column drifts from the tender by up to {abs(d) * 100:.2f}% "
                         f"(largest on {t.code or 'line ' + str(t.sl_no)}: {_qty(b)} vs {_qty(a)} sqft"
                         + (f"; it converts with {factor:.4f} instead of {SQFT_PER_SQM}" if factor else "")
                         + "). Small, but it shows up when bills are reconciled.")
    for t in tender:
        if not t.measured:
            notes.append(f"{t.code or 'Line ' + str(t.sl_no)} ({t.description}) is marked “{t.note or 'not measured'}” on the site sheet, "
                         f"so the tender's {_qty(t.qty)} {t.unit} is not a measurement. It is left out of the comparable total.")
    return notes


# ── Per vendor ─────────────────────────────────────────────────────────────────────────────────
def _drift_flag(vq: float, tq: float, unit: str) -> tuple[str | None, float]:
    r = vq / tq - 1
    if abs(r) <= SAME:
        return None, r
    if abs(r) <= ROUNDING:
        return f"Quantity {_qty(vq)} vs tender {_qty(tq)} {unit} ({r * 100:+.2f}%): rounding drift.", r
    return f"Priced on {_qty(vq)} {unit}, tender is {_qty(tq)} ({r * 100:+.0f}%).", r


def compare_one(tender: list[TenderLine], q: Quote) -> VendorCompare:
    mine = {ln.sl_no: ln for ln in q.lines}
    lines: list[LineCompare] = []
    vflags: list[str] = []
    ratios: list[float] = []
    complete = True
    for t in tender:
        ln = mine.get(t.sl_no)
        if ln is not None:
            ln.code = t.code
        flags: list[str] = []
        if ln is None:
            lines.append(LineCompare(sl_no=t.sl_no, code=t.code, vendor_unit=None, vendor_qty=None, rate=None, rate_per_tender_unit=None,
                                     repriced=None, as_quoted=None, comparable=t.measured, flags=["Not quoted."]))
            if t.measured:
                complete = False
            continue
        as_quoted = ln.amount if ln.amount is not None else (ln.qty * ln.rate if ln.qty is not None and ln.rate is not None else None)
        rate_tu = convert_rate(ln.rate, ln.unit, t.unit) if ln.rate is not None else None
        comparable = t.measured
        if not t.measured:
            flags.append("Not measured on the site sheet: not comparable.")
        if ln.unit != t.unit:
            if rate_tu is None:
                flags.append(f"Quoted per {ln.unit_as_written}, tender is per {t.unit.upper()}: units don't convert.")
                comparable = False
                if t.measured:
                    complete = False
            else:
                flags.append(f"Quoted per {ln.unit_as_written}, tender is per {t.unit.upper()}: rate converted.")
        if ln.rate is None:
            flags.append("No rate read.")
            if t.measured:
                complete = False
            comparable = False
        if ln.qty is not None and t.qty:
            if ln.unit == t.unit:
                f, r = _drift_flag(ln.qty, t.qty, t.unit)
                if f:
                    flags.append(f)
                if t.measured:
                    ratios.append(r)
        if ln.arithmetic_ok is False:
            flags.append("Printed amount isn't qty x rate.")
        repriced = rate_tu * t.qty if comparable and rate_tu is not None and t.qty is not None else None
        lines.append(LineCompare(sl_no=t.sl_no, code=t.code, vendor_unit=ln.unit, vendor_qty=ln.qty, rate=ln.rate, rate_per_tender_unit=rate_tu,
                                 repriced=repriced, as_quoted=as_quoted, comparable=comparable and repriced is not None, flags=flags))

    extra = sorted(set(mine) - {t.sl_no for t in tender})
    if extra:
        vflags.append(f"Lines not in the tender: {', '.join(map(str, extra))}.")
    if ratios:
        worst = max(ratios, key=abs)
        basis = "tender" if abs(worst) <= ROUNDING else "different"
        if basis == "different":
            avg = sum(ratios) / len(ratios)
            vflags.append(f"Priced on a different quantity set: on average {avg * 100:+.0f}% vs the tender on the comparable lines.")
        elif abs(worst) > SAME:
            vflags.append(f"Quantities follow the tender with rounding drift (up to {abs(worst) * 100:.2f}%).")
    else:
        basis = "unknown"
    q.quantity_basis = basis
    if q.needs_vision:
        vflags.append("Rates not readable from the text layer: needs a person or a vision model.")
        complete = False

    priced_q = [ln.as_quoted for ln in lines if ln.as_quoted is not None]
    as_quoted_total = sum(priced_q) if priced_q and not q.needs_vision else None
    rp = [ln.repriced for ln in lines if ln.comparable and ln.repriced is not None]
    repriced_total = sum(rp) if rp and not q.needs_vision else None
    if not complete and not q.needs_vision:
        vflags.append("Some comparable lines are missing or unreadable, so the re-priced total is partial.")
    return VendorCompare(vendor=q.vendor, file=q.file, read_confidence=q.read_confidence, needs_vision=q.needs_vision, quantity_basis=basis,
                         as_quoted_total=as_quoted_total, repriced_total=repriced_total, complete=complete, lines=lines, flags=vflags)


def _rank(items: list[VendorCompare], attr: str, rank_attr: str) -> None:
    ok = sorted((v for v in items if getattr(v, attr) is not None and (attr == "as_quoted_total" or v.complete)), key=lambda v: getattr(v, attr))
    for i, v in enumerate(ok, 1):
        setattr(v, rank_attr, i)


def compare(tender: list[TenderLine], quotes: list[Quote]) -> Comparison:
    vendors = [compare_one(tender, q) for q in quotes]
    _rank(vendors, "as_quoted_total", "rank_as_quoted")
    _rank(vendors, "repriced_total", "rank_repriced")
    ranked = sorted((v for v in vendors if v.rank_repriced), key=lambda v: v.rank_repriced)
    if ranked:
        low = ranked[0].repriced_total
        for v in ranked:
            v.pct_above_lowest = round(100 * (v.repriced_total / low - 1), 1) if low else None

    # Lines that can't be compared across vendors, with what each vendor quoted.
    non: list[NonComparable] = []
    by_sl = {ln.sl_no: ln for q in quotes for ln in q.lines}  # just to know a line exists
    for t in tender:
        mine = [(q, ln) for q in quotes for ln in q.lines if ln.sl_no == t.sl_no]
        rows = [{"vendor": q.vendor, "unit": ln.unit_as_written, "qty": ln.qty, "rate": ln.rate} for q, ln in mine]
        units = {ln.unit.upper() for _, ln in mine}
        qtys = {r["qty"] for r in rows if r["qty"] is not None}
        reasons = []
        if not t.measured:
            reasons.append(f"not measured on the site sheet (“{t.note or 'not measured'}”)")
        if len(units) > 1:
            reasons.append(f"vendors quoted it per {', '.join(sorted(units))}")
        if not t.measured and len(qtys) > 1:
            reasons.append(f"on quantities {', '.join(_qty(x) for x in sorted(qtys))}")
        if reasons and (not t.measured or t.sl_no in by_sl):
            non.append(NonComparable(sl_no=t.sl_no, code=t.code, description=t.description,
                                     reason="Not comparable: " + "; ".join(reasons) + ".", quoted=rows))

    comparable_codes = [t.code or str(t.sl_no) for t in tender if t.measured]
    insights: list[str] = []
    cq = next((v for v in vendors if v.rank_as_quoted == 1), None)
    if ranked and cq is not None and cq.rank_repriced is None:
        insights.append(f"{cq.vendor} looks cheapest as quoted ({inr(cq.as_quoted_total)}), but it doesn't price every comparable line, "
                        f"so its total isn't comparable. Lowest on the tender quantities: {ranked[0].vendor} at {inr(ranked[0].repriced_total)} "
                        "(complete quotes only).")
    elif ranked and cq is not None and cq.rank_repriced != 1:
        other = [v.vendor for v in ranked if v is not cq and v.quantity_basis == "different"]
        why = (" It priced a different quantity set from the tender, so its PDF total isn't comparable." if cq.quantity_basis == "different"
               else f" {' and '.join([', '.join(other[:-1]), other[-1]] if len(other) > 1 else other)} priced a different quantity set, so {'their PDF totals aren' if len(other) > 1 else 'its PDF total isn'}'t comparable."
               if other else "")
        insights.append(
            f"{cq.vendor} looks cheapest as quoted ({inr(cq.as_quoted_total)}), but on the tender quantities it is "
            + (f"{cq.pct_above_lowest:.0f}% above the lowest" if cq.pct_above_lowest is not None else "not the lowest")
            + f" ({ranked[0].vendor}, {inr(ranked[0].repriced_total)})." + why)
    elif ranked:
        insights.append(f"Lowest on the tender quantities: {ranked[0].vendor} at {inr(ranked[0].repriced_total)} (comparable lines only).")
    for v in vendors:
        if v.needs_vision:
            insights.append(f"{v.vendor}'s quote is a scan without readable rates: it isn't ranked until someone reads it.")
    for n in non:
        insights.append(f"{n.code or 'Line ' + str(n.sl_no)}: {n.reason[0].lower()}{n.reason[1:]} Ask for a measured quantity before comparing it.")
    return Comparison(tender=tender, comparable_codes=comparable_codes, vendors=vendors, non_comparable=non, insights=insights,
                      notes=tender_notes(tender))
