"""scripts/make_demo_vendor_data.py: deterministic demo data, tagged, and removable. Made-up vendors; tmp files only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from procurement.shortlist import deals as D
from procurement.shortlist import score as S
from procurement.vendors import store as V

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_vendor_data.py"
T = "2026-01-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("make_demo_vendor_data", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_files(tmp_path: Path) -> tuple[Path, Path]:
    wp = [V.Vendor(id=f"wp{i}", name=f"Wetproof {i}", categories=["Waterproofing"], created_at=T, updated_at=T) for i in range(4)]
    wp[0].credit_days = 15  # a real value: must survive
    other = [V.Vendor(id="el1", name="Spark Electric", categories=["Electrical"], created_at=T, updated_at=T),
             V.Vendor(id="bad", name="Blocked Co", status="blocked", created_at=T, updated_at=T)]
    loose = [V.Vendor(id=f"u{i:02d}", name=f"Loose Vendor {i}", created_at=T, updated_at=T) for i in range(25)]
    deals = [D.Deal(id=f"r{i}{c}", vendor_id=f"wp{i}", category="Waterproofing", project="Hillside Tower-1", item_code=c,
                    unit="rmt" if c == "WP.03" else "sqft", rate=r + i, date_quoted="2026-05-15", source="quote.pdf")
             for i in range(4) for c, r in (("WP.01", 34), ("WP.02", 34), ("WP.03", 50))]
    vp, dp = tmp_path / "vendors.json", tmp_path / "deals.json"
    vp.write_text(json.dumps({"version": 1, "vendors": [v.model_dump(mode="json") for v in wp + other + loose]}))
    dp.write_text(json.dumps({"version": 1, "deals": [d.model_dump(mode="json") for d in deals]}))
    return vp, dp


def norm(vp: Path) -> list[dict]:
    return [V.Vendor.model_validate(v).model_dump(mode="json") for v in json.loads(vp.read_text())["vendors"]]


def test_generate_is_deterministic_tagged_and_keeps_real_data(gen, tmp_path):
    vp, dp = make_files(tmp_path)
    real_deals = json.loads(dp.read_text())["deals"]
    gen.run(vp, dp, quiet=True)
    first_v, first_d = vp.read_text(), dp.read_text()
    assert list(tmp_path.glob("vendors.json.bak-*")) and list(tmp_path.glob("deals.json.bak-*"))
    gen.run(vp, dp, quiet=True)  # rerun: strips its own data first, then regenerates the same
    assert vp.read_text() == first_v and dp.read_text() == first_d

    deals = json.loads(dp.read_text())["deals"]
    assert [d for d in deals if d["source"] != "demo"] == real_deals  # real quotes untouched
    demo_deals = [d for d in deals if d["source"] == "demo"]
    assert demo_deals and all(d["id"].startswith("demo-") for d in demo_deals)
    # No demo quote on a code the vendor already quoted for real.
    real_keys = {(d["vendor_id"], d["item_code"]) for d in real_deals}
    assert not real_keys & {(d["vendor_id"], d["item_code"]) for d in demo_deals}

    vs = {v["id"]: v for v in norm(vp)}
    assert vs["wp0"]["credit_days"] == 15 and "credit_days" not in vs["wp0"]["demo_fields"]  # real value kept
    for v in vs.values():
        assert all(r["source"] == "demo" for r in v["performance"])
        for k in v["demo_fields"]:
            assert v[k] is not None
    assert vs["bad"]["performance"] == [] and vs["bad"]["demo_categories"] == []
    assert vs["el1"]["performance"] and vs["el1"]["credit_days"] is not None  # other categories: history + credit
    added = {c: [v for v in vs.values() if c in v["demo_categories"]] for c in gen.DEMO_REQS}
    assert len(added["Waterproofing"]) == 3 and len(added["Cement"]) == 4
    for c, lst in added.items():
        assert all(c in v["categories"] for v in lst)


def test_scores_spread_with_a_thin_vendor(gen, tmp_path, monkeypatch):
    vp, dp = make_files(tmp_path)
    gen.run(vp, dp, quiet=True)
    monkeypatch.setenv("VENDORS_PATH", str(vp))
    monkeypatch.setenv("VENDOR_DEALS_PATH", str(dp))
    for cat in gen.DEMO_REQS:
        res = S.shortlist(cat)
        scores = [v.score for v in res.vendors]
        assert len(set(scores)) == len(scores) >= 4, cat
        assert res.selected == 3
        assert all(v.demo for v in res.vendors)
    wp = S.shortlist("Waterproofing").vendors
    assert min(v.coverage for v in wp) < 0.5 and sorted(v.coverage for v in wp)[1] == 1.0  # one thin vendor


def test_remove_round_trip(gen, tmp_path):
    vp, dp = make_files(tmp_path)
    before_v, before_d = norm(vp), json.loads(dp.read_text())
    gen.run(vp, dp, quiet=True)
    assert norm(vp) != before_v
    gen.run(vp, dp, remove=True, quiet=True)
    assert norm(vp) == before_v and json.loads(dp.read_text()) == before_d


def test_paths_from_args_and_env(gen, monkeypatch, tmp_path):
    monkeypatch.setenv("VENDORS_PATH", str(tmp_path / "a.json"))
    monkeypatch.setenv("VENDOR_DEALS_PATH", str(tmp_path / "b.json"))
    ns = lambda **kw: type("A", (), {"vendors": None, "deals": None} | kw)()  # noqa: E731
    assert gen.paths(ns()) == (tmp_path / "a.json", tmp_path / "b.json")
    assert gen.paths(ns(vendors="x.json"))[0] == Path("x.json")

