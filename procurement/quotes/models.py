"""Shapes for step 4 (read quotes) and the re-priced comparison."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Confidence = Literal["high", "medium", "low"]


class QuoteLine(BaseModel):
    sl_no: int
    code: str | None = None  # item code, filled in when matched to the tender
    description: str
    unit: str  # normalised: sqft, sqm, rmt, nos, kg, mt …
    unit_as_written: str
    qty: float | None
    rate: float | None  # rupees per unit, before GST
    amount: float | None  # as printed, if the quote has an amount column
    page: int  # 1-based
    arithmetic_ok: bool | None = None  # amount == qty x rate (None when there is no amount to check)


class Term(BaseModel):
    key: str  # gst, validity_days, payment, lead_time_days, warranty
    value: str
    page: int
    text: str  # the line it was read from, verbatim


class Quote(BaseModel):
    vendor: str  # from the file name
    file: str
    producer: str = ""
    date: str | None = None  # YYYY-MM-DD
    lines: list[QuoteLine]
    terms: list[Term] = []
    quantity_basis: str = "unknown"  # "tender" | "different" | "unknown", set by compare
    read_confidence: Confidence
    needs_vision: bool = False
    notes: list[str] = []

    def term(self, key: str) -> str | None:
        return next((t.value for t in self.terms if t.key == key), None)


class TenderLine(BaseModel):
    sl_no: int
    code: str | None
    description: str
    unit: str
    qty: float | None
    measured: bool = True  # False when the site sheet says it wasn't measured
    sheet_sqft: float | None = None  # site sheet sqm x 10.7639, for the drift check
    sheet_sqft_written: float | None = None  # the site sheet's own sqft column
    note: str | None = None


class LineCompare(BaseModel):
    sl_no: int
    code: str | None
    vendor_unit: str | None
    vendor_qty: float | None
    rate: float | None
    rate_per_tender_unit: float | None
    repriced: float | None  # rate x tender quantity
    as_quoted: float | None  # vendor qty x rate (or the printed amount)
    comparable: bool
    flags: list[str] = []


class VendorCompare(BaseModel):
    vendor: str
    file: str
    read_confidence: Confidence
    needs_vision: bool
    quantity_basis: str
    as_quoted_total: float | None
    repriced_total: float | None  # comparable lines only
    complete: bool  # every comparable tender line priced
    rank_as_quoted: int | None = None
    rank_repriced: int | None = None
    pct_above_lowest: float | None = None  # on the re-priced total
    lines: list[LineCompare]
    flags: list[str] = []


class NonComparable(BaseModel):
    sl_no: int
    code: str | None
    description: str
    reason: str
    quoted: list[dict]  # [{vendor, unit, qty, rate}]


class Comparison(BaseModel):
    tender: list[TenderLine]
    comparable_codes: list[str]
    vendors: list[VendorCompare]
    non_comparable: list[NonComparable]
    insights: list[str]
    notes: list[str]
