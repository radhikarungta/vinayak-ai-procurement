"""Vendor directory: store, seed and API. Uses a temp store and a made-up data pack; no network."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from procurement.vendors import seed, store
from procurement.vendors.api import router

# Made-up names, shaped like the real list (spacing, legal suffixes, a name that overlaps a quote).
LIST_NAMES = ["R K CONSTRUCTION", "NORTHWIND SALES & SERVICES PRIVATE LIMITED", "Ray Electricals Pvt. Ltd.",
              "ACME TESTING LABORATORY", "BLUE FURNITURE", "J K ENTERPRISE"]
QUOTES = ["Northwind", "RK_Construction", "Riverbank_Construction"]


def make_pack(root, names=LIST_NAMES, quotes=QUOTES):
    (root / "05_vendors").mkdir(parents=True)
    (root / "06_quotations").mkdir()
    wb = Workbook()
    ws = wb.active
    ws.title = "Contractor List"
    ws.append(["Contractor"])
    for n in names:
        ws.append([n])
    ws.append([None])
    wb.save(root / "05_vendors" / "Contractor_list_Hillside.xlsx")
    for q in quotes:
        (root / "06_quotations" / f"Quotation_{q}.pdf").write_bytes(b"%PDF-1.4 stub")
    return root


@pytest.fixture
def paths(tmp_path, monkeypatch):
    vp = tmp_path / "out" / "vendors.json"
    monkeypatch.setenv("VENDORS_PATH", str(vp))
    monkeypatch.setenv("VENDORS_PACK", str(tmp_path / "pack"))
    return tmp_path / "pack", vp


@pytest.fixture
def client(paths):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def rec(**kw):
    base = {"date": "2026-10-01", "project": "Hillside Tower-2", "work": "Toilet waterproofing", "quality": 4,
            "delivery": 3, "disputes": "none", "note": "", "recorded_by": "Site engineer"}
    return base | kw


# ── names, GSTIN, scoring ─────────────────────────────────────────────────────────────────────
def test_name_normalisation():
    assert store.norm_name("R K CONSTRUCTION") == store.norm_name("RK Construction") == "rk construction"
    assert store.norm_name("Foo Pvt. Ltd.") == store.norm_name("FOO PRIVATE LIMITED") == "foo"


def test_gstin_validation():
    assert store.VendorFields(gstin="19abcde1234f1z5").gstin == "19ABCDE1234F1Z5"
    with pytest.raises(ValueError):
        store.VendorFields(gstin="12345")
    assert store.VendorFields(gstin="").gstin == ""


def test_no_history_is_not_zero():
    s = store.performance_summary([])
    assert s["count"] == 0 and s["quality"] is None and s["delivery"] is None and s["label"] == "no history"
    s = store.performance_summary([rec(quality=4, delivery=2), rec(quality=5, delivery=3, disputes="major")])
    assert (s["count"], s["quality"], s["delivery"], s["disputes"]["major"]) == (2, 4.5, 2.5, 1)
    assert "(2 records)" in s["label"]


# ── seed ──────────────────────────────────────────────────────────────────────────────────────
def test_seed_merges_and_is_idempotent(paths):
    pack, vp = paths
    make_pack(pack)
    r = seed.run(quiet=True)
    assert r["vendors"] == 7  # 6 on the list + 3 quotes − 2 already on the list
    assert r["in_both_sources"] == 2 and r["merged_this_run"] == 2
    vs = {v.name: v for v in store.load_all()}
    sk = vs["R K CONSTRUCTION"]
    assert sk.sources == [seed.LIST_SOURCE, seed.QUOTE_SOURCE]
    assert sk.categories == ["Waterproofing"] and sk.categories_to_confirm == []
    assert set(sk.projects) == {"Hillside", seed.QUOTE_PROJECT}
    assert "Waterproofing" in vs["NORTHWIND SALES & SERVICES PRIVATE LIMITED"].categories
    assert vs["Riverbank Construction"].sources == [seed.QUOTE_SOURCE]
    # Guessed from the name → marked to confirm.
    assert vs["Ray Electricals Pvt. Ltd."].categories_to_confirm == ["Electrical"]
    assert vs["ACME TESTING LABORATORY"].categories_to_confirm == ["Testing & lab"]
    assert vs["J K ENTERPRISE"].categories == []
    assert all(v.status == "on list, not vetted" for v in vs.values())

    before = vp.read_text()
    r2 = seed.run(quiet=True)
    assert (r2["vendors"], r2["created"], r2["updated"]) == (7, 0, 0)
    assert json.loads(vp.read_text())["vendors"] == json.loads(before)["vendors"]


def test_seed_keeps_human_edits(paths):
    pack, _ = paths
    make_pack(pack)
    seed.run(quiet=True)
    v = store.find_by_name(store.load_all(), "Ray Electricals")
    store.update(v.id, store.VendorFields(categories_to_confirm=[], status="approved", phone="98300 00000"))
    seed.run(quiet=True)
    v = store.get(v.id)
    assert v.categories_to_confirm == [] and v.status == "approved" and v.phone == "98300 00000"


def test_seed_without_pack(paths):
    _, vp = paths
    r = seed.run(quiet=True)
    assert r["vendors"] == 0 and "No data pack found" in r["message"]
    assert vp.exists() and store.load_all() == []


def test_first_load_seeds_automatically(paths):
    pack, vp = paths
    make_pack(pack)
    assert not vp.exists()
    assert len(store.load_all()) == 7 and vp.exists()


# ── API ───────────────────────────────────────────────────────────────────────────────────────
def test_api_crud_and_filters(client, paths):
    make_pack(paths[0])
    r = client.get("/api/vendors")
    assert r.status_code == 200 and r.json()["count"] == 7

    assert {v["name"] for v in client.get("/api/vendors", params={"category": "waterproofing"}).json()["items"]} == {
        "R K CONSTRUCTION", "NORTHWIND SALES & SERVICES PRIVATE LIMITED", "Riverbank Construction"}
    assert [v["name"] for v in client.get("/api/vendors", params={"q": "rk construction"}).json()["items"]] == ["R K CONSTRUCTION"]
    assert client.get("/api/vendors", params={"status": "approved"}).json()["count"] == 0
    assert client.get("/api/vendors", params={"status": "nope"}).status_code == 400

    new = client.post("/api/vendors", json={"name": "Delta Waterproofing", "categories": ["Waterproofing"], "gstin": "19ABCDE1234F1Z5",
                                            "email": "rfq@delta.example", "status": "approved", "city": "Kolkata"})
    assert new.status_code == 201
    vid = new.json()["id"]
    assert new.json()["performance_summary"]["label"] == "no history" and new.json()["has_contact"] is True
    assert client.post("/api/vendors", json={"name": "DELTA WATERPROOFING PVT LTD"}).status_code == 409
    assert client.post("/api/vendors", json={"name": "Bad GST", "gstin": "XYZ"}).status_code == 422
    assert client.post("/api/vendors", json={"name": "  "}).status_code == 400

    up = client.patch(f"/api/vendors/{vid}", json={"phone": "033 4000 0000", "status": "blocked"})
    assert up.status_code == 200 and up.json()["status"] == "blocked" and up.json()["city"] == "Kolkata"
    assert client.get(f"/api/vendors/{vid}").json()["phone"] == "033 4000 0000"

    cats = {c["name"]: c for c in client.get("/api/vendors/categories").json()}
    assert cats["Waterproofing"]["vendors"] == 4 and cats["Electrical"]["to_confirm"] == 1

    assert client.delete(f"/api/vendors/{vid}").status_code == 204
    assert client.get(f"/api/vendors/{vid}").status_code == 404
    assert client.delete(f"/api/vendors/{vid}").status_code == 404


def test_api_performance(client, paths):
    vid = client.post("/api/vendors", json={"name": "Echo Builders"}).json()["id"]
    r = client.post(f"/api/vendors/{vid}/performance", json=rec(quality=5, delivery=4))
    assert r.status_code == 201
    r = client.post(f"/api/vendors/{vid}/performance", json=rec(date="2026-11-01", quality=3, delivery=2, disputes="minor"))
    body = r.json()
    assert body["performance_summary"]["count"] == 2
    assert body["performance_summary"]["quality"] == 4.0 and body["performance_summary"]["delivery"] == 3.0
    assert body["performance"][0]["date"] == "2026-11-01"  # newest first
    assert "Hillside Tower-2" in body["projects"]
    assert client.post(f"/api/vendors/{vid}/performance", json=rec(quality=6)).status_code == 422
    assert client.post(f"/api/vendors/{vid}/performance", json=rec(disputes="some")).status_code == 422
    assert client.post("/api/vendors/missing/performance", json=rec()).status_code == 404


def test_api_summary(client, paths):
    s = client.get("/api/vendors/summary").json()
    assert s["ready"] is False and s["stats"][0] == {"label": "Vendors", "value": 0, "tone": None}
    vid = client.post("/api/vendors", json={"name": "Foxtrot", "categories": ["Electrical"], "phone": "1"}).json()["id"]
    client.post("/api/vendors", json={"name": "Golf"})
    client.post(f"/api/vendors/{vid}/performance", json=rec())
    s = client.get("/api/vendors/summary").json()
    assert set(s) == {"title", "headline", "stats", "ready"}
    stats = {x["label"]: x for x in s["stats"]}
    assert stats["Vendors"]["value"] == 2
    assert stats["With a category"] == {"label": "With a category", "value": 1, "tone": "warn"}
    assert stats["With contact details"]["value"] == 1 and stats["With past performance"]["value"] == 1
    assert all(x["tone"] in {"good", "warn", "bad", None} for x in s["stats"])
    assert s["ready"] is True


def test_api_credit_terms(client, paths):
    vid = client.post("/api/vendors", json={"name": "Hotel Supplies"}).json()["id"]
    v = client.get(f"/api/vendors/{vid}").json()
    assert v["credit_days"] is None and v["advance_pct"] is None and v["credit_label"] == "not known"
    v = client.patch(f"/api/vendors/{vid}", json={"credit_days": 45, "advance_pct": 10}).json()
    assert (v["credit_days"], v["advance_pct"], v["credit_label"]) == (45, 10, "45 days credit, 10% advance")
    # Editing another field leaves credit terms alone; null clears them back to "not known" (not 0).
    assert client.patch(f"/api/vendors/{vid}", json={"city": "Howrah"}).json()["credit_days"] == 45
    v = client.patch(f"/api/vendors/{vid}", json={"credit_days": None, "advance_pct": None}).json()
    assert v["credit_days"] is None and v["advance_pct"] is None
    assert client.patch(f"/api/vendors/{vid}", json={"credit_days": 0}).json()["credit_label"] == "advance / on delivery"
    assert client.patch(f"/api/vendors/{vid}", json={"credit_days": -1}).status_code == 422
    assert client.patch(f"/api/vendors/{vid}", json={"advance_pct": 101}).status_code == 422


def test_editing_a_demo_value_makes_it_real(paths):
    t = store.now()
    v = store.Vendor(id="v1", name="India Demo", categories=["Cement", "Steel"], credit_days=30, advance_pct=20,
                     demo_fields=["credit_days", "advance_pct"], demo_categories=["Cement", "Steel"], created_at=t, updated_at=t)
    store.save_all([v])
    v = store.update("v1", store.VendorFields(credit_days=60, categories=["Steel"]))
    assert v.demo_fields == ["advance_pct"] and v.demo_categories == ["Steel"]
    out = store.public(v)
    assert out["demo_fields"] == ["advance_pct"] and out["performance_summary"]["demo"] == 0


def test_summary_counts_demo_records():
    recs = [rec() | {"source": "demo"}, rec()]
    assert store.performance_summary(recs)["demo"] == 1
