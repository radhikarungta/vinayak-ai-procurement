"""Read a site's quantity sheet (.xlsx) into BOQ lines. Deterministic: no LLM touches numbers.

Site sheets differ, but the work-order abstract always has a header row naming the code, the
description and the unit (UOM). We find that row, map columns by their header text, and read
down until a GRAND TOTAL row. Columns between UOM and the TOTAL columns are the breakdown
(flat-wise, floor-wise, whatever the site used).
"""

from __future__ import annotations

import io
import re

from openpyxl import load_workbook

from .models import BoqLine, QuantitySheet


def _norm(v: object) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().upper()


def _num(v: object) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _find_header(ws) -> tuple[int, dict[str, int], list[tuple[str, int]]] | None:
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30), values_only=True), start=1):
        cells = [_norm(c) for c in row]
        code = next((i for i, c in enumerate(cells) if "CODE" in c), None)
        uom = next((i for i, c in enumerate(cells) if c in ("UOM", "UNIT")), None)
        desc = next((i for i, c in enumerate(cells) if c.startswith("DESCRIPTION")), None)
        if code is None or uom is None or desc is None:
            continue
        cols = {"code": code, "uom": uom, "desc": desc}
        for i, c in enumerate(cells):
            if c.startswith("SL"):
                cols["sl"] = i
            elif "ITEMS INCLUDED" in c:
                cols["items"] = i
            elif c.startswith("TOTAL") and "SQM" in c:
                cols["sqm"] = i
            elif c.startswith("TOTAL") and ("SFT" in c or "SQFT" in c):
                cols["sqft"] = i
            elif c.startswith("REMARK"):
                cols["remarks"] = i
        end = cols.get("sqm", len(cells))
        breakdown = [(str(row[i]).strip(), i) for i in range(uom + 1, end) if row[i] not in (None, "")]
        return r, cols, breakdown
    return None


def parse_quantity_sheet(data: bytes, filename: str) -> QuantitySheet | None:
    """Return the first sheet that looks like a work-order quantity abstract, or None."""
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        found = _find_header(ws)
        if not found:
            continue
        header_row, cols, breakdown = found
        title_bits = [
            str(row[0]).strip()
            for row in ws.iter_rows(min_row=1, max_row=header_row - 1, values_only=True)
            if row and row[0]
        ]
        lines: list[BoqLine] = []
        gt_sqm = gt_sqft = None
        gt_row = None
        for r, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            if not any(c not in (None, "") for c in row):
                continue
            first = _norm(row[0])
            if "GRAND TOTAL" in first or "GRAND TOTAL" in _norm(row[cols["desc"]]):
                gt_sqm = _num(row[cols["sqm"]]) if "sqm" in cols else None
                gt_sqft = _num(row[cols["sqft"]]) if "sqft" in cols else None
                gt_row = r
                break
            desc = row[cols["desc"]]
            if not desc:
                continue
            sqm = _num(row[cols["sqm"]]) if "sqm" in cols else None
            sqft = _num(row[cols["sqft"]]) if "sqft" in cols else None
            remarks = str(row[cols["remarks"]]).strip() if "remarks" in cols and row[cols["remarks"]] else None
            bd = {name: v for name, i in breakdown if (v := _num(row[i])) is not None}
            measured = sqm is not None and not (remarks and "NOT MEASURED" in remarks.upper())
            lines.append(
                BoqLine(
                    sl_no=str(row[cols["sl"]]) if "sl" in cols and row[cols["sl"]] is not None else None,
                    code=str(row[cols["code"]]).strip() if row[cols["code"]] else None,
                    description=str(desc).strip(),
                    items_included=(str(row[cols["items"]]).strip() if "items" in cols and row[cols["items"]] not in (None, "-") else None),
                    uom=str(row[cols["uom"]]).strip() if row[cols["uom"]] else None,
                    breakdown=bd,
                    total_sqm=sqm,
                    total_sqft=sqft,
                    remarks=remarks,
                    measured=measured,
                    row=r,
                )
            )
        if lines:
            return QuantitySheet(
                filename=filename,
                sheet=ws.title,
                title=" · ".join(title_bits) or None,
                lines=lines,
                breakdown_columns=[n for n, _ in breakdown],
                grand_total_sqm=gt_sqm,
                grand_total_sqft=gt_sqft,
                grand_total_row=gt_row,
            )
    return None
