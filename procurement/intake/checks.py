"""BOQ checks for SOP step 02 ("preliminary scrutiny of the submitted BOQ"). Plain code, no LLM.

Each check explains itself in a sentence the back office can forward to site as-is.
"""

from __future__ import annotations

from .email_parser import Email
from .models import Check, ExtractedRequirement, QuantitySheet

SQFT_PER_SQM = 10.7639
MIN_QUOTES = 3  # Vinayak SOP step 05


def _fmt(n: float, d: int = 2) -> str:
    """Indian digit grouping: 39,896.00 → '39,896.00'; 1234567 → '12,34,567.00'."""
    neg = n < 0
    s = f"{abs(n):.{d}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + (f".{frac}" if d else "")


def run_checks(mail: Email, req: ExtractedRequirement, sheet: QuantitySheet | None) -> list[Check]:
    checks: list[Check] = []

    # 1. A vendor is named before the tender.
    if req.vendor_named.value:
        basis = f" ({req.vendor_named_basis.value})" if req.vendor_named_basis.value else ""
        overruled = any("tender" in t.position.lower() or "compar" in t.position.lower() for t in req.thread)
        checks.append(
            Check(
                id="vendor-before-tender",
                severity="block",
                title="The request names a vendor before tendering",
                detail=(
                    f"The email proposes {req.vendor_named.value}{basis}. SOP step 05 needs quotes from at least "
                    f"{MIN_QUOTES} vendors, so this goes to tender. The named vendor is kept on record so the "
                    "approver sees it next to the comparative."
                    + (" A later reply in the thread already says the same." if overruled else "")
                ),
                source=f'Email: "{req.vendor_named.quote}"',
            )
        )

    # 2. The email mentions an attachment but none came through.
    mentions_sheet = bool(req.attachments_referenced.values) or "attached" in mail.body.lower()
    if mentions_sheet and sheet is None:
        checks.append(
            Check(
                id="attachment-missing",
                severity="block",
                title="The quantity sheet is missing or unreadable",
                detail="The email says quantities are attached, but no Excel quantity sheet could be read. Ask site to resend it as .xlsx.",
                source="Attachments: " + (", ".join(a.filename for a in mail.attachments) or "none"),
            )
        )

    # 3. Missing basics.
    missing = [label for label, f in (("project", req.project), ("type of work", req.work_type), ("start date", req.start), ("finish date", req.finish)) if not f.value]
    if not req.scope_areas.values:
        missing.append("scope areas")
    if missing:
        checks.append(
            Check(
                id="missing-fields",
                severity="warn",
                title="Some basics aren't in the email",
                detail="Not stated: " + ", ".join(missing) + ". Confirm with site before the tender document is drafted.",
                source="Email",
            )
        )

    if sheet is None:
        return checks

    where = f"{sheet.filename} › {sheet.sheet}"

    # 4. Lines with no measurement.
    unmeasured = [ln for ln in sheet.lines if not ln.measured]
    if unmeasured:
        names = "; ".join(f"{ln.code or ln.sl_no} {ln.description.capitalize()}" for ln in unmeasured)
        checks.append(
            Check(
                id="unmeasured-lines",
                severity="warn",
                title=f"{len(unmeasured)} line{'s are' if len(unmeasured) > 1 else ' is'} not measured",
                detail=f"{names}. Ask site for these quantities, or carry a provisional figure in the tender and say so.",
                source=where + " rows " + ", ".join(str(ln.row) for ln in unmeasured),
            )
        )

    # 5. sqm → sqft conversion factor.
    measured = [ln for ln in sheet.lines if ln.total_sqm and ln.total_sqft and (ln.uom or "").upper() in ("SQM", "SQ.M", "M2")]
    if measured:
        factors = {round(ln.total_sqft / ln.total_sqm, 4) for ln in measured}
        used = sorted(factors)
        off = [f for f in used if abs(f - SQFT_PER_SQM) > 0.001]
        if off:
            total_sqm = sum(ln.total_sqm for ln in measured)
            sheet_sqft = sum(ln.total_sqft for ln in measured)
            true_sqft = total_sqm * SQFT_PER_SQM
            big = max(measured, key=lambda ln: ln.total_sqm)
            checks.append(
                Check(
                    id="sqft-factor",
                    severity="warn",
                    title="The sheet converts sqm to sqft with a rounded factor",
                    detail=(
                        f"The sheet multiplies by {', '.join(f'{f:g}' for f in used)}. The exact factor is 10.7639. "
                        f"Across all lines that is {_fmt(sheet_sqft)} sqft instead of {_fmt(true_sqft)} "
                        f"({_fmt(true_sqft - sheet_sqft)} sqft short). On {big.code} alone: {_fmt(big.total_sqft)} vs "
                        f"{_fmt(big.total_sqm * SQFT_PER_SQM)} sqft. Small, but it shows up when bills are reconciled."
                    ),
                    source=where,
                )
            )
        else:
            checks.append(Check(id="sqft-factor", severity="ok", title="sqm → sqft conversion is exact", detail="Every line uses 10.7639.", source=where))

    # 6. Breakdown columns add up to each line's total.
    bad_rows = []
    for ln in sheet.lines:
        if ln.breakdown and ln.total_sqm is not None:
            s = sum(ln.breakdown.values())
            if abs(s - ln.total_sqm) > 0.01:
                bad_rows.append(f"{ln.code}: parts add to {_fmt(s)}, total says {_fmt(ln.total_sqm)}")
    if bad_rows:
        checks.append(Check(id="row-sums", severity="warn", title="Some lines don't add up", detail="; ".join(bad_rows) + ".", source=where))
    elif any(ln.breakdown for ln in sheet.lines):
        cols = ", ".join(sheet.breakdown_columns)
        checks.append(Check(id="row-sums", severity="ok", title="Every line adds up across its breakdown", detail=f"Columns checked: {cols}".rstrip(".") + ".", source=where))

    # 7. Lines add up to the grand total.
    if sheet.grand_total_sqm is not None:
        s = sum(ln.total_sqm or 0 for ln in sheet.lines)
        if abs(s - sheet.grand_total_sqm) > 0.01:
            checks.append(
                Check(
                    id="grand-total",
                    severity="warn",
                    title="The lines don't add up to the grand total",
                    detail=f"Lines add to {_fmt(s)} sqm; the grand total row says {_fmt(sheet.grand_total_sqm)} sqm.",
                    source=f"{where} row {sheet.grand_total_row}",
                )
            )
        else:
            checks.append(
                Check(
                    id="grand-total",
                    severity="ok",
                    title="Lines add up to the grand total",
                    detail=f"{_fmt(s)} sqm across {len(sheet.lines)} lines.",
                    source=f"{where} row {sheet.grand_total_row}",
                )
            )

    order = {"block": 0, "warn": 1, "ok": 2}
    return sorted(checks, key=lambda c: order[c.severity])
