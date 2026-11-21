"""Checks against the real data pack in data/pack (git-ignored). Skipped when the pack isn't there.

No real names in this file: vendors are whatever the quotation file names say, read at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement.itemcodes import api as ic_api
from procurement.quotes import api as q_api
from procurement.quotes import pack
from procurement.quotes.parse import vendor_from_filename

PACK = Path(__file__).resolve().parent.parent / "data" / "pack"
MASTER = PACK / "04_item_codes" / "ERP_item_codes_waterproofing.xlsx"
QUOTES = PACK / "06_quotations"

pytestmark = pytest.mark.skipif(not (MASTER.exists() and QUOTES.is_dir()), reason="real data pack not in data/pack")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ITEM_MASTER_PATH", str(MASTER))
    monkeypatch.setenv("QUOTES_PACK", str(PACK))
    app = FastAPI()
    app.include_router(ic_api.router)
    app.include_router(q_api.router)
    return TestClient(app)


def test_item_master_rows_shifted_by_one(client):
    d = client.get("/api/itemcodes/duplicates").json()
    assert d["ready"] is True
    shifted = {m["code"] for m in d["misplaced"]}
    assert {"WP.03.B", "WP.04.B", "WP.05.B", "WP.06.B", "WP.07.B"} <= shifted
    # Each of those carries the text of the next code.
    for m in d["misplaced"]:
        if m["code"] in {"WP.03.B", "WP.04.B", "WP.05.B"}:
            nxt = f"WP.0{int(m['code'][4]) + 1}"
            assert any(r.startswith(nxt) for r in m["reads_like"]), m
    assert any("shifted by one row" in n and "WP.03.B" in n and "WP.07.B" in n for n in d["notes"])
    assert d["counts"]["same_code_different_text"] >= 3


def test_quote_parser_reads_the_three_text_pdfs(client):
    files = sorted(QUOTES.glob("Quotation_*.pdf"))
    assert len(files) == 4
    quotes = pack.read_quotes()
    assert {q.vendor for q in quotes} == {vendor_from_filename(f) for f in files}
    text = [q for q in quotes if not q.needs_vision]
    scans = [q for q in quotes if q.needs_vision]
    assert len(text) == 3 and len(scans) == 1
    assert scans[0].read_confidence == "low"
    for q in text:
        assert [ln.sl_no for ln in q.lines] == list(range(1, 8)), q.file
        assert all(ln.rate and ln.qty for ln in q.lines), q.file
        assert q.read_confidence in {"high", "medium"}


def test_repricing_on_the_real_pack(client):
    d = client.get("/api/quotes/comparison").json()
    assert d["ready"] and "WP.03" not in d["comparable_codes"] and len(d["comparable_codes"]) == 6
    assert any(n["code"] == "WP.03" for n in d["non_comparable"])
    ranked = [v for v in d["vendors"] if v["rank_repriced"]]
    assert len(ranked) == 3
    cheapest_quoted = next(v for v in d["vendors"] if v["rank_as_quoted"] == 1)
    # The trap: the cheapest-looking quote is the only one on tender quantities, and not the cheapest re-priced.
    assert cheapest_quoted["quantity_basis"] == "tender" and cheapest_quoted["rank_repriced"] != 1
    assert cheapest_quoted["pct_above_lowest"] > 20
    assert sum(v["quantity_basis"] == "different" for v in ranked) == 2
    # The SQM-vs-RMT line is flagged on the vendor who quoted it that way.
    assert any("units don't convert" in f for v in d["vendors"] for ln in v["lines"] if ln["code"] == "WP.03" for f in ln["flags"])
    s = client.get("/api/quotes/summary").json()
    assert s["ready"] and "looks cheapest as quoted" in s["headline"]
