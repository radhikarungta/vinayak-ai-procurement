"""Vendor shortlist: scoring, ranking and API. Made-up vendors and rates; temp stores; no network, no LLM."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement.shortlist import deals as D
from procurement.shortlist import score as S
from procurement.shortlist.api import router
from procurement.vendors import store as V

T = "2026-01-01T00:00:00+00:00"


def vendor(vid, name, cats=("Waterproofing",), status="on list, not vetted", perf=()):
    return V.Vendor(id=vid, name=name, categories=list(cats), status=status, created_at=T, updated_at=T,
                    performance=[V.Performance(id=f"{vid}-{i}", recorded_at=T, date="2026-01-01", project="Hillside",
                                               work="Toilets", quality=q, delivery=d, recorded_by="Site engineer")
                                 for i, (q, d) in enumerate(perf)])


def deal(vid, code, rate, unit="sqft", **kw):
    base = dict(vendor_id=vid, project="Hillside Tower-1", category="Waterproofing", item_code=code, unit=unit, rate=rate)
    return D.Deal(**(base | kw), id=f"{vid}-{code}-{rate}", response_hours=D.response_hours(kw.get("rfq_sent_at"), kw.get("date_quoted")))


def by_name(res):
    return {v.name: v for v in res.vendors}


def factor(v, key):
    return next(f for f in v.factors if f.key == key)


# ── no data, re-normalisation, coverage ───────────────────────────────────────────────────────
def test_no_data_is_not_zero_and_weights_renormalise():
    vs = [vendor("a", "Alder Works", perf=[(5, 5)]), vendor("n", "Newbrook Co")]
    res = S.shortlist("Waterproofing", vendors=vs, deals=[])
    a, n = by_name(res)["Alder Works"], by_name(res)["Newbrook Co"]
    # Only performance (5/5) and experience (1 job = 0.6) have data for Alder.
    assert factor(a, "price").score is None and "No rate" in factor(a, "price").reason
    exp = 100 * (0.25 * 1.0 + 0.10 * 0.6) / (0.25 + 0.10)
    assert a.score == pytest.approx(round(exp, 1))
    assert a.coverage == pytest.approx(0.35)
    # A vendor with no data at all has no score (not 0), zero coverage, and is not pre-ticked.
    assert n.score is None and n.coverage == 0 and not n.selected
    assert all(f.score is None for f in n.factors)


def test_combine_and_coverage():
    fs = [S.Factor(key="price", label="", weight=0.35, score=1.0, reason=""),
          S.Factor(key="response", label="", weight=0.15, score=0.0, reason=""),
          S.Factor(key="lead_time", label="", weight=0.15, score=None, reason="")]
    score, cov = S.combine(fs)
    assert score == pytest.approx(70.0)  # 0.35 / (0.35 + 0.15)
    assert cov == pytest.approx(round(0.5 / 0.65, 3))
    assert S.combine([S.Factor(key="price", label="", weight=1, score=None, reason="")]) == (None, 0.0)


# ── price vs median ───────────────────────────────────────────────────────────────────────────
def test_median_price_comparison_and_unit_mismatch():
    vs = [vendor(x, f"Vendor {x.upper()}") for x in "abcd"]
    ds = [deal("a", "WP.01", 30), deal("b", "WP.01", 40), deal("c", "WP.01", 50),
          deal("a", "WP.03", 50, "rmt"), deal("b", "WP.03", 60, "rmt"), deal("d", "WP.03", 30, "sqm")]
    res = S.shortlist("Waterproofing", vendors=vs, deals=ds)
    assert res.medians["WP.01"] == {"unit": "sqft", "median": 40, "quotes": 3}
    assert res.medians["WP.03"] == {"unit": "rmt", "median": 55, "quotes": 2}
    r = by_name(res)
    # B is exactly at the median on WP.01 → 0.5; C is 25% above → 0 (clamped).
    assert factor(r["Vendor C"], "price").score == 0.0
    assert "25% above the median" in factor(r["Vendor C"], "price").reason
    b = factor(r["Vendor B"], "price")
    assert b.score == pytest.approx((0.5 + S._linear(60 / 55, 0.8, 1.2)) / 2, abs=1e-3)
    # D quoted per sqm against an rmt median: not compared, and the reason says so.
    d = factor(r["Vendor D"], "price")
    assert d.score is None and "per sqm" in d.reason


def test_latest_rate_wins_and_single_rate_has_no_median():
    vs = [vendor("a", "Vendor A"), vendor("b", "Vendor B")]
    ds = [deal("a", "WP.01", 90, date_quoted="2026-01-01"), deal("a", "WP.01", 40, date_quoted="2026-06-01"),
          deal("b", "WP.01", 40, date_quoted="2026-06-01"), deal("a", "WP.02", 10)]
    res = S.shortlist("Waterproofing", vendors=vs, deals=ds)
    assert res.medians["WP.01"]["median"] == 40
    assert "WP.02" not in res.medians and any("WP.02" in n for n in res.notes)


def test_response_and_lead_time_reasons():
    vs = [vendor("a", "Vendor A")]
    ds = [deal("a", "WP.01", 40, rfq_sent_at="2026-06-01T10:00", date_quoted="2026-06-02T10:00", lead_time_days_promised=7),
          deal("a", "WP.02", 40, rfq_sent_at="2026-06-01T10:00", date_quoted="2026-06-02T10:00", lead_time_days_promised=7)]
    a = S.shortlist("Waterproofing", vendors=vs, deals=ds).vendors[0]
    assert factor(a, "response").score == 1.0 and "24 hours" in factor(a, "response").reason and "1 quote" in factor(a, "response").reason
    assert factor(a, "lead_time").score == 1.0 and "7 days" in factor(a, "lead_time").reason


# ── ranking, ties, pre-tick ───────────────────────────────────────────────────────────────────
def test_top_three_pre_ticked():
    vs = [vendor(x, f"Vendor {x.upper()}", perf=[(q, q)]) for x, q in zip("abcde", (5, 4, 3, 2, 1))]
    res = S.shortlist("Waterproofing", vendors=vs, deals=[])
    assert [v.name for v in res.vendors] == ["Vendor A", "Vendor B", "Vendor C", "Vendor D", "Vendor E"]
    assert [v.selected for v in res.vendors] == [True, True, True, False, False]
    assert [v.rank for v in res.vendors] == [1, 2, 3, 4, 5]
    assert res.selected == 3 and not any(v.tied for v in res.vendors)


def test_tie_at_third_place_ticks_all_tied():
    vs = [vendor(x, f"Vendor {x.upper()}", perf=[(q, q)]) for x, q in zip("abcde", (5, 4, 3, 3, 1))]
    res = S.shortlist("Waterproofing", vendors=vs, deals=[])
    r = by_name(res)
    assert r["Vendor C"].rank == r["Vendor D"].rank == 3
    assert r["Vendor C"].tied and r["Vendor D"].tied
    assert r["Vendor C"].selected and r["Vendor D"].selected and not r["Vendor E"].selected
    assert r["Vendor E"].rank == 5
    assert res.selected == 4 and any("tie" in n for n in res.notes)


def test_equal_score_breaks_on_coverage_then_name():
    mk = lambda n, sc, cov: S.Scored(vendor_id=n, name=n, status="approved", categories=[], score=sc, coverage=cov, factors=[])  # noqa: E731
    out = S.rank([mk("Zed", 60.0, 0.5), mk("Amy", 60.0, 0.9), mk("Bob", 60.0, 0.5), mk("Cat", None, 0.0)])
    assert [(s.name, s.rank, s.tied) for s in out] == [("Amy", 1, False), ("Bob", 2, True), ("Zed", 2, True), ("Cat", 4, False)]
    assert [s.selected for s in out] == [True, True, True, False]


# ── eligibility ───────────────────────────────────────────────────────────────────────────────
def test_blocked_excluded_and_other_categories_ignored():
    vs = [vendor("a", "Vendor A"), vendor("b", "Vendor B", status="blocked"), vendor("e", "Sparks Electric", cats=["Electrical"]),
          vendor("q", "Quoter Q", cats=[])]
    res = S.shortlist("Waterproofing", vendors=vs, deals=[deal("q", "WP.01", 40)])
    names = [v.name for v in res.vendors]
    assert "Vendor B" not in names and [x.name for x in res.excluded] == ["Vendor B"]
    assert "blocked" in res.excluded[0].reason.lower()
    assert "Sparks Electric" not in names
    # Not listed under the category, but quoted the codes: kept, with a note.
    assert any("Not listed under" in n for n in by_name(res)["Quoter Q"].notes)
    assert any("at least 3" in n for n in res.notes)  # only 2 eligible


def test_requirements_lists_categories_with_codes():
    vs = [vendor("a", "Vendor A"), vendor("e", "Sparks Electric", cats=["Electrical"])]
    reqs = S.requirements(deals=[deal("a", "WP.01", 40, item_text="Toilet floor")], vendors=vs)
    assert reqs[0] == {"category": "Waterproofing", "vendors": 1, "item_codes": [{"code": "WP.01", "text": "Toilet floor"}]}


# ── API ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDORS_PATH", str(tmp_path / "vendors.json"))
    monkeypatch.setenv("VENDORS_PACK", str(tmp_path / "no-pack"))
    monkeypatch.setenv("VENDOR_DEALS_PATH", str(tmp_path / "deals.json"))
    monkeypatch.setenv("VENDOR_DEALS_SEED", str(tmp_path / "no-seed.json"))
    V.save_all([vendor(x, f"Vendor {x.upper()}", perf=[(q, 4)]) for x, q in zip("abcd", (5, 4, 3, 2))]
               + [vendor("z", "Vendor Z", status="blocked")])
    D.save_all([deal("a", "WP.01", 40), deal("b", "WP.01", 44)])
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_shortlist_and_summary(client):
    r = client.get("/api/shortlist", params={"category": "Waterproofing", "codes": "wp.01"}).json()
    assert r["item_codes"] == ["WP.01"] and r["min_rfq"] == 3
    assert [v["selected"] for v in r["vendors"]] == [True, True, True, False]
    assert r["excluded"][0]["name"] == "Vendor Z"
    assert "pre-ticked" in r["headline"]
    s = client.get("/api/shortlist/summary").json()
    assert set(s) == {"title", "headline", "stats", "ready"} and s["ready"] is True
    assert {st["label"]: st["value"] for st in s["stats"]}["Pre-ticked"] == 3
    assert client.get("/api/shortlist").json()["category"] == "Waterproofing"


def test_api_deals_crud(client):
    d = client.post("/api/shortlist/deals", json={"vendor_id": "c", "category": "Waterproofing", "item_code": "WP.01", "unit": "sq ft", "rate": 41}).json()
    assert d["unit"] == "sqft" and client.get("/api/shortlist/deals", params={"vendor_id": "c"}).json()["count"] == 1
    assert client.post("/api/shortlist/deals", json={"vendor_id": "nope", "category": "X", "unit": "sqft", "rate": 1}).status_code == 404
    assert client.delete(f"/api/shortlist/deals/{d['id']}").status_code == 204
