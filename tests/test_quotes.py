"""Quote reading and re-pricing (step 4). Made-up vendors and figures; no PDFs, no network."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement.intake.checks import SQFT_PER_SQM
from procurement.quotes import api as q_api
from procurement.quotes import compare as C
from procurement.quotes import pack
from procurement.quotes.models import Quote, QuoteLine, TenderLine
from procurement.quotes.parse import build_quote, norm_unit, parse_lines


def tender() -> list[TenderLine]:
    return [
        TenderLine(sl_no=1, code="WP.01", description="FLOOR", unit="sqft", qty=1000.0),
        TenderLine(sl_no=2, code="WP.02", description="WALL", unit="sqft", qty=500.0),
        TenderLine(sl_no=3, code="WP.03", description="DEPRESSION", unit="rmt", qty=100.0, measured=False, note="Not measured"),
    ]


def quote(vendor: str, rows: list[tuple[int, str, float, float]], **kw) -> Quote:
    lines = [QuoteLine(sl_no=sl, description=f"LINE {sl}", unit=norm_unit(u), unit_as_written=u, qty=q, rate=r, amount=None, page=1)
             for sl, u, q, r in rows]
    return Quote(vendor=vendor, file=f"Quotation_{vendor}.pdf", lines=lines, read_confidence=kw.pop("confidence", "high"), **kw)


# ── Parsing layout text ────────────────────────────────────────────────────────────────────────
PAGE = """
                         Riverbank Waterproofers                               12.03.2026
SL NO.   ITEM DESCRIPTION                              UNIT      QUANTITY     RATE       AMOUNT
   1     WATERPROOFING AT TOILET FLOOR                 SQFT      1,000.00     40.00      40,000.00
         Surface preparation and two coats of membrane, 1mm wet film.
   2     WATERPROOFING AT BALCONY      & TERRACE WALL  SQ.FT       500.00     42.50      21,250.00
   3     WATERPROOFING ON TOILET DEPRESSION            SQM.        100.00     30.00       3,000.00
         Total                                                                         64,250.00
Terms
1. GST @ 18% as applicable.
2. 25% payment to be given along with the order.
3. Validity of our offer: 30 days from the date of quotation.
4. We can start within 10 days of the work order.
"""


def test_parse_rows_units_amounts_and_terms():
    q = build_quote("Riverbank Waterproofers", "Quotation_Riverbank.pdf", [PAGE], "Microsoft Word")
    assert [ln.sl_no for ln in q.lines] == [1, 2, 3]
    assert [ln.unit for ln in q.lines] == ["sqft", "sqft", "sqm"]
    assert q.lines[1].description == "WATERPROOFING AT BALCONY & TERRACE WALL"
    assert (q.lines[0].qty, q.lines[0].rate, q.lines[0].amount) == (1000.0, 40.0, 40000.0)
    assert all(ln.arithmetic_ok for ln in q.lines)
    assert q.date == "2026-03-12" and q.read_confidence == "high" and not q.needs_vision
    assert q.term("gst") == "18% extra" and q.term("validity_days") == "30" and q.term("lead_time_days") == "10"
    assert "25%" in q.term("payment")


def test_indian_grouping_and_bad_arithmetic_lowers_confidence():
    page = "  1  FLOOR WORK      SQFT   13,429.53   47.00   6,31,187.91\n  2  WALL WORK       SQFT   100.00   10.00   9,999.00\n"
    q = build_quote("X", "x.pdf", [page])
    assert q.lines[0].amount == 631187.91 and q.lines[0].arithmetic_ok
    assert q.lines[1].arithmetic_ok is False
    assert q.read_confidence == "medium"


def test_scan_without_rates_needs_vision_and_is_not_guessed():
    page = "  1|WATERPROOFING AT TOILET FLOOR        SOFT          30,420.00\n  2 WATERPROOFING AT WALL     SQFT\n"
    q = build_quote("Scanned Co", "scan.pdf", [page], "Adobe Scan for Android")
    assert q.needs_vision and q.read_confidence == "low"
    assert all(ln.rate is None for ln in q.lines)
    assert q.lines[0].unit == "sqft"  # OCR "SOFT"


def test_no_amount_column_is_medium_confidence():
    q = build_quote("Y", "y.pdf", ["  1   FLOOR       SQFT   1,000.00   34.50\n"])
    assert q.lines[0].rate == 34.5 and q.lines[0].amount is None
    assert q.read_confidence == "medium"


def test_parse_lines_ignores_repeated_page_header_rows():
    lines = parse_lines(["  1  FLOOR    SQFT   10.00   2.00   20.00\n", "  1  FLOOR    SQFT   99.00   9.00   891.00\n"])
    assert len(lines) == 1 and lines[0].qty == 10.0


# ── Re-pricing ─────────────────────────────────────────────────────────────────────────────────
def test_reprices_on_tender_quantities_and_catches_the_trap():
    # Alpha quotes the tender quantities at a higher rate; Beta quotes an older, 2.5x quantity set at a lower rate.
    alpha = quote("Alpha Seal", [(1, "SQFT", 1000, 50), (2, "SQFT", 500, 50), (3, "RMT", 100, 50)])
    beta = quote("Beta Coat", [(1, "SQFT", 2500, 40), (2, "SQFT", 1250, 40), (3, "RMT", 250, 80)])
    cmp = C.compare(tender(), [alpha, beta])
    a, b = (next(v for v in cmp.vendors if v.vendor == n) for n in ("Alpha Seal", "Beta Coat"))
    assert a.as_quoted_total == 80000 and b.as_quoted_total == 170000
    assert a.rank_as_quoted == 1
    assert a.repriced_total == 75000 and b.repriced_total == 60000  # WP.03 left out
    assert b.rank_repriced == 1 and a.rank_repriced == 2
    assert a.pct_above_lowest == 25.0
    assert a.quantity_basis == "tender" and b.quantity_basis == "different"
    assert "Alpha Seal looks cheapest as quoted" in cmp.insights[0] and "25% above the lowest" in cmp.insights[0]
    assert cmp.comparable_codes == ["WP.01", "WP.02"]
    wp03 = next(n for n in cmp.non_comparable if n.code == "WP.03")
    assert "not measured" in wp03.reason and {r["vendor"] for r in wp03.quoted} == {"Alpha Seal", "Beta Coat"}


def test_sqm_rate_is_converted_but_running_metre_is_not():
    t = tender()
    t[1] = TenderLine(sl_no=2, code="WP.02", description="WALL", unit="sqft", qty=500.0)
    q = quote("Gamma Works", [(1, "SQFT", 1000, 40), (2, "SQM.", 46.45, 430.0), (3, "SQM.", 100, 30)])
    v = C.compare(t, [q]).vendors[0]
    wall = next(ln for ln in v.lines if ln.code == "WP.02")
    assert wall.comparable and wall.rate_per_tender_unit == pytest.approx(430 / SQFT_PER_SQM)
    assert any("rate converted" in f for f in wall.flags)
    dep = next(ln for ln in v.lines if ln.code == "WP.03")
    assert not dep.comparable and any("don't convert" in f for f in dep.flags)
    assert v.complete  # WP.03 isn't measured, so it doesn't make the total partial


def test_unit_mismatch_on_a_measured_line_makes_total_partial():
    t = [TenderLine(sl_no=1, code="A", description="X", unit="rmt", qty=10.0)]
    v = C.compare(t, [quote("Delta", [(1, "SQM", 10, 5)])]).vendors[0]
    assert not v.complete and v.rank_repriced is None
    assert any("partial" in f for f in v.flags)


def test_rounding_drift_is_flagged_but_still_tender_basis():
    q = quote("Epsilon", [(1, "SQFT", 1004, 10), (2, "SQFT", 500, 10)])
    v = C.compare(tender(), [q]).vendors[0]
    floor = next(ln for ln in v.lines if ln.code == "WP.01")
    assert any("rounding drift" in f for f in floor.flags)
    assert v.quantity_basis == "tender"


def test_missing_line_and_vision_quote_are_not_ranked():
    partial = quote("Zeta", [(1, "SQFT", 1000, 10)])
    scan = quote("Eta", [(1, "SQFT", 1000, None)], confidence="low", needs_vision=True)
    full = quote("Theta", [(1, "SQFT", 1000, 12), (2, "SQFT", 500, 12)])
    cmp = C.compare(tender(), [partial, scan, full])
    by = {v.vendor: v for v in cmp.vendors}
    assert by["Zeta"].rank_repriced is None and not by["Zeta"].complete
    assert by["Eta"].repriced_total is None and by["Eta"].as_quoted_total is None
    assert by["Theta"].rank_repriced == 1
    assert any("Eta" in s and "scan" in s for s in cmp.insights)


def test_inr_uses_indian_grouping():
    assert C.inr(1875112.3) == "₹18,75,112"
    assert C.inr(999) == "₹999" and C.inr(None) == "–"


def test_api_without_a_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path))
    app = FastAPI()
    app.include_router(q_api.router)
    c = TestClient(app)
    s = c.get("/api/quotes/summary").json()
    assert s["ready"] is False and s["title"] == q_api.TITLE
    assert c.get("/api/quotes").json()["items"] == []
    assert pack.files()["quotes"] == []


# ── Review fixes ───────────────────────────────────────────────────────────────────────────────
def test_every_units_spelling_and_dash_placeholders_parse():
    page = ("  1  FLOOR WORK      SQ.MT   10.00   5.00   50.00\n"
            "  2  ANCHOR BOLTS    NO      4       2.00   8.00\n"
            "  3  STEEL           MT      1.00    -      -\n"
            "  4  EDGE TRIM       R.M.T.  3.00    2.00   6.00\n")
    lines = parse_lines([page])
    assert [(ln.sl_no, ln.unit) for ln in lines] == [(1, "sqm"), (2, "nos"), (3, "mt"), (4, "rmt")]
    assert (lines[2].qty, lines[2].rate, lines[2].amount) == (1.0, None, None)


def test_a_boq_row_that_does_not_parse_is_noted_not_dropped_silently():
    page = "  1  FLOOR WORK    SQFT   10.00   2.00   20.00\n  2  SITE CLEANING    LS   1.00   500.00   500.00\n"
    q = build_quote("Kestrel Builders", "k.pdf", [page])
    assert [ln.sl_no for ln in q.lines] == [1]
    assert q.read_confidence == "medium" and any("couldn't be read" in n and "SITE CLEANING" in n for n in q.notes)


def test_two_figures_follow_the_header_rate_and_amount():
    page = ("SL   DESCRIPTION        UNIT     RATE     AMOUNT\n"
            "  1  FLOOR WORK         SQFT     40.00    40,000.00\n")
    q = build_quote("Heron Seal", "h.pdf", [page])
    ln = q.lines[0]
    assert (ln.qty, ln.rate, ln.amount) == (1000.0, 40.0, 40000.0) and ln.arithmetic_ok is None
    assert q.read_confidence == "medium" and any("amount / rate" in n for n in q.notes)


def test_two_figures_without_a_header_are_flagged_as_a_guess():
    q = build_quote("Y", "y.pdf", ["  1   FLOOR       SQFT   1,000.00   34.50\n"])
    assert (q.lines[0].qty, q.lines[0].rate) == (1000.0, 34.5)
    assert q.read_confidence == "medium" and any("no column header" in n for n in q.notes)


def test_an_unreadable_pdf_is_a_low_confidence_quote_not_an_error(tmp_path, monkeypatch):
    d = tmp_path / "06_quotations"
    d.mkdir()
    (d / "Quotation_Osprey_Coat.pdf").write_bytes(b"not a pdf")
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path))
    [q] = pack.read_quotes()
    assert q.vendor == "Osprey Coat" and q.needs_vision and q.read_confidence == "low" and "couldn't be read" in q.notes[0]
    app = FastAPI()
    app.include_router(q_api.router)
    c = TestClient(app)
    assert c.get("/api/quotes").status_code == 200 and c.get("/api/quotes/summary").status_code == 200


def test_headline_when_the_cheapest_as_quoted_is_incomplete():
    short = quote("Wren Works", [(1, "SQFT", 1000, 10)])  # WP.02 missing
    full = quote("Lark Linings", [(1, "SQFT", 1000, 12), (2, "SQFT", 500, 12)])
    cmp = C.compare(tender(), [short, full])
    assert "Wren Works looks cheapest as quoted" in cmp.insights[0] and "doesn't price every comparable line" in cmp.insights[0]
    assert "different quantity set" not in cmp.insights[0]


def test_headline_only_blames_quantities_that_actually_differ():
    # Both on tender quantities; Kite is cheaper as quoted only because of the unmeasured WP.03.
    kite = quote("Kite Seal", [(1, "SQFT", 1000, 50), (2, "SQFT", 500, 50), (3, "RMT", 10, 50)])
    rook = quote("Rook Coat", [(1, "SQFT", 1000, 45), (2, "SQFT", 500, 45), (3, "RMT", 400, 50)])
    cmp = C.compare(tender(), [kite, rook])
    assert "Kite Seal looks cheapest as quoted" in cmp.insights[0] and "different quantity set" not in cmp.insights[0]


def test_zero_lowest_total_has_no_percentage():
    cmp = C.compare(tender(), [quote("Free Co", [(1, "SQFT", 1000, 0), (2, "SQFT", 500, 0)]),
                               quote("Paid Co", [(1, "SQFT", 1000, 1), (2, "SQFT", 500, 1)])])
    assert all(v.pct_above_lowest is None for v in cmp.vendors)


def test_generated_seed_is_not_taken_for_the_hand_made_one():
    assert not pack.SEED_PATH.match("*quotes*.json")


def test_sync_drops_old_deals_of_a_pack_file_that_is_now_a_scan(tmp_path, monkeypatch):
    from pathlib import Path

    from procurement.shortlist import deals as D

    monkeypatch.setenv("VENDOR_DEALS_PATH", str(tmp_path / "deals.json"))
    deal = dict(vendor_id="v1", category="Waterproofing", unit="sqft", rate=10)
    D.save_all([D.Deal(id="a", source="Quotation_Acme.pdf", **deal), D.Deal(id="b", source="hand-read.pdf", **deal)])
    monkeypatch.setattr(pack, "files", lambda: {"quotes": [Path("Quotation_Acme.pdf")], "tender": None, "sheet": None})
    monkeypatch.setattr(pack, "write_seed", lambda: tmp_path / "seed.json")
    monkeypatch.setattr(D, "seed_deals", lambda path=None: [])  # the PDF was replaced by a scan: no parsed rates
    out = pack.sync_deals()
    assert out["files"] == ["Quotation_Acme.pdf"] and [d.id for d in D.load_all()] == ["b"]
