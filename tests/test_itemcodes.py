"""Item codes (step 2). Runs on a small made-up master, so the real data pack isn't needed. No network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from procurement.intake import extract
from procurement.intake.quantity_sheet import parse_quantity_sheet
from procurement.itemcodes import api as ic_api
from procurement.itemcodes import master as M
from procurement.itemcodes import match as X

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

# Each duplicate kind from the real master, in miniature. Sheet rows start at 3.
ROWS = [
    ("WP.01", "WATERPROOFING AT TOILET FLOOR"),                       # 3
    ("WP.02", "WATERPROOFING AT TOILET WALL (UPTO 450 MM HIGHT)"),    # 4
    ("WP.04", "WATERPROOFING AT KITCHEN FLOOR"),                      # 5
    ("WP.05", "WATERPROOFING AT KITCHEN WALL"),                       # 6
    ("WP.06", "WATERPROOFING AT BALCONY FLOOR"),                      # 7
    ("WP.08", "PRESSURE GROUTING INSIDE R.C.C FLOOR & WALL"),         # 8
    ("WP.01.A", "WATERPROOFING AT TOILET FLOOR-A"),                   # 9  repeated
    ("WP.01.A", "WATERPROOFING AT TOILET FLOOR-A"),                   # 10 repeated
    ("WP.04.B", "WATERPROOFING AT KITCHEN FLOOR-B"),                  # 11 same code, two texts
    ("WP.04.B", "WATERPROOFING AT KITCHEN WALL-B"),                   # 12 … and shifted onto WP.05's text
    ("WP.EXP.01", "Expansion Joint Polysulphide Treatment"),          # 13 same code, two texts
    ("WP.EXP.01", "Hypalon Expansion Joint Treatment"),               # 14
    ("WP.11.B", "*NOZZLE GROUTING (IF REQUIRED)"),                    # 15 near: WP.11.B_
    ("WP.11.B_", "NOZZLE GROUTING (*IF REQUIRED)"),                   # 16
    ("STP_UGR", "Nozzle grouting at all construction joints"),        # 17 near: W.P.03
    ("W.P.03", "Grouting at all construction joints"),                # 18
    ("WP.RC", "CONCRETE PUMPING WORK (SCREED CONCRETE)"),             # 19 near: WP.RC1
    ("WP.RC1", "CONCRETE PUMPING WORK (SCREED CONCRETE)-1"),          # 20
    ("W.P-COBA", "WATERPROOFING WITH BRICK BAT COBA"),                # 21
    ("WP.MR", "Scaffolding charges"),                                 # 22
    ("W.P.06", "EXPENSION JOINT ALUMINIUM SHEET FIXING"),             # 23
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    monkeypatch.setattr(extract, "_client", lambda: pytest.fail("no network calls in tests"))


@pytest.fixture
def xlsx(tmp_path, monkeypatch) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "ERP ITEM CODE"
    ws.append(["ERP ITEM CODE for Water-proofing work", None, None])
    ws.append(["Sl No", "Item Code", "Item Description"])
    for i, (code, desc) in enumerate(ROWS, start=1):
        ws.append([i, code, desc])
    p = tmp_path / "master.xlsx"
    wb.save(p)
    monkeypatch.setenv("ITEM_MASTER_PATH", str(p))
    return p


@pytest.fixture
def master(xlsx) -> M.Master:
    return M.load_master()


def by(report, kind):
    return [g for g in report.groups if g.kind == kind]


# ── normalising and loading ────────────────────────────────────────────────────────────────────


def test_normalise_fixes_case_typos_suffix_and_punctuation():
    assert M.normalise("Waterproofing at toilet wall (upto 450 mm HIGHT)-A") == "WATERPROOFING AT TOILET WALL UPTO 450 MM HEIGHT"
    assert M.normalise("EXPENSION JOINT") == "EXPANSION JOINT"
    assert M.normalise("WATERPROOFINWORKS {BALCONY WALL}") == "WATERPROOFING WORKS BALCONY WALL"
    assert M.normalise("UGR Outside Wall Area-Methodology-1") != M.normalise("UGR Outside Wall Area-Methodology-2")
    assert "RCC" in M.normalise("INSIDE R.C.C FLOOR")


def test_synonyms_fold_site_words():
    assert {"TOILET"} <= M.tokens("W.C. and bathrooms")
    assert {"UGR", "GROUTING"} <= M.tokens("grout the underground reservoir")
    assert {"COBA", "ROOF"} <= M.tokens("brick bat coba")
    assert "BALCONY" in M.tokens("F.R.P.") and "SKYTERRACE" in M.tokens("sky terrace")


def test_load_master_keeps_source_rows(master, xlsx):
    assert master.path == str(xlsx)
    assert len(master.entries) == len(ROWS)
    first = master.entries[0]
    assert (first.row, first.sl_no, first.code) == (3, "1", "WP.01")
    assert [e.row for e in master.entries if e.code == "WP.04.B"] == [11, 12]


def test_missing_master_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ITEM_MASTER_PATH", str(tmp_path / "nope.xlsx"))
    with pytest.raises(M.MasterMissing, match="ITEM_MASTER_PATH"):
        M.get_master()


# ── duplicate report ───────────────────────────────────────────────────────────────────────────


def test_repeated_rows(master):
    rep = M.duplicate_report(master)
    rep_codes = {g.codes[0]: g.members[0].rows for g in by(rep, "repeated")}
    assert rep_codes == {"WP.01.A": [9, 10]}


def test_same_code_different_text(master):
    rep = M.duplicate_report(master)
    groups = {g.codes[0]: g for g in by(rep, "same_code_different_text")}
    assert set(groups) == {"WP.04.B", "WP.EXP.01"}
    assert {m.text for m in groups["WP.EXP.01"].members} == {"Expansion Joint Polysulphide Treatment", "Hypalon Expansion Joint Treatment"}


def test_near_duplicates_under_different_codes(master):
    rep = M.duplicate_report(master)
    sets = [set(g.codes) for g in by(rep, "near_duplicate")]
    for pair in ({"WP.11.B", "WP.11.B_"}, {"STP_UGR", "W.P.03"}, {"WP.RC", "WP.RC1"}, {"WP.01", "WP.01.A"}):
        assert any(pair <= s for s in sets), pair
    # Toilet floor and toilet wall are different items, not duplicates.
    assert not any({"WP.01", "WP.02"} <= s for s in sets)


def test_variant_on_the_wrong_text_is_flagged(master):
    rep = M.duplicate_report(master)
    m = next(x for x in rep.misplaced if x.code == "WP.04.B")
    assert m.row == 12 and "WP.05" in m.reads_like


# ── matching ───────────────────────────────────────────────────────────────────────────────────


def line(text, **kw):
    return X.RequirementLine(id=kw.pop("id", "1"), text=text, **kw)


def test_exact_match_with_reason(master):
    m = X.match_line(line("WATERPROOFING AT TOILET WALL (UPTO 450 MM HEIGHT)"), master)
    assert (m.kind, m.code) == ("exact", "WP.02")
    assert "WP.02" in m.reason and m.erp_rows == [4]


def test_base_code_beats_variants_and_odd_codes(master):
    assert X.match_line(line("waterproofing at toilet floor"), master).code == "WP.01"
    assert X.match_line(line("nozzle grouting if required"), master).code == "WP.11.B"


def test_partial_wider_scope_suggests_an_unused_variant(master):
    m = X.match_line(line("WATERPROOFING AT KITCHEN & WASH PART FLOOR"), master)
    assert (m.kind, m.code) == ("partial", "WP.04")
    assert "wash" in m.reason
    assert m.suggestion and m.suggestion.code not in master.codes() and m.suggestion.code.startswith("WP.04.")


def test_toilets_means_floor_and_wall(master):
    m = X.match_line(line("Internal waterproofing: Toilets"), master)
    assert m.kind == "partial" and m.code == "WP.01" and m.also == ["WP.02"]


def test_none_suggests_a_new_unused_code(master):
    m = X.match_line(line("OPC 53 cement, 400 bags"), master)
    assert m.kind == "none" and m.code is None
    assert m.suggestion.code == "CEM.01"
    wp = X.match_line(line("Waterproofing at lift lobby floor"), master)
    assert wp.kind == "none" and wp.suggestion.code.startswith("WP.") and wp.suggestion.code not in master.codes()


def test_scaffolding_only_partly_matches_the_waterproofing_list(master):
    m = X.match_line(line("Cuplock scaffolding on hire"), master)
    assert (m.kind, m.code) == ("partial", "WP.MR")
    assert "waterproofing" in m.reason and m.suggestion.code == "SCF.01"


def test_brick_bat_coba_is_the_roof_item(master):
    m = X.match_line(line("Roof waterproofing with brick bat coba: Main roof"), master)
    assert (m.kind, m.code) == ("exact", "W.P-COBA")


def test_site_code_that_means_something_else_in_the_erp(master):
    m = X.match_line(line("NOZZLE GROUTING (IF REQUIRED)", site_code="WP.08"), master)
    assert m.code == "WP.11.B"
    bad = [f for f in m.flags if f.tone == "bad"]
    assert bad and "PRESSURE GROUTING" in bad[0].text
    unknown = X.match_line(line("WATERPROOFING AT TOILET FLOOR", site_code="WP.99"), master)
    assert any("isn't in the ERP list" in f.text for f in unknown.flags)
    ok = X.match_line(line("WATERPROOFING AT TOILET FLOOR", site_code="WP.01"), master)
    assert ok.flags[0].tone == "good"


def test_conflicting_code_is_flagged_when_chosen(master):
    m = X.match_line(line("Hypalon expansion joint treatment"), master)
    assert m.code == "WP.EXP.01"
    assert any(f.tone == "bad" and "meanings" in f.text for f in m.flags)


# ── lines from a demo inbox record ─────────────────────────────────────────────────────────────


def test_lines_from_quantity_sheet(master):
    sheet = parse_quantity_sheet((SAMPLES / "T2_waterproofing_quantities.xlsx").read_bytes(), "T2.xlsx")
    lines = X.lines_from_record({"uid": "9", "result": {"quantities": sheet.model_dump(mode="json")}}, master)
    assert len(lines) == len(sheet.lines)
    assert lines[0].site_code == "WP.01" and lines[0].quantity == sheet.lines[0].total_sqm and lines[0].source.startswith("Quantity sheet row")


def test_lines_from_requirement_split_by_scope_area(master):
    recs = {r["uid"]: r for r in json.loads((SAMPLES / "demo_inbox.json").read_text())}
    toilets = X.lines_from_record(recs["1011"], master)
    assert [ln.text for ln in toilets] == ["Internal waterproofing: Toilets", "Internal waterproofing: Kitchens", "Internal waterproofing: Balconies"]
    scaff = X.lines_from_record(recs["1004"], master)
    assert len(scaff) == 1 and "North face" in scaff[0].text


# ── Claude chooses from the shortlist ──────────────────────────────────────────────────────────


def fake_client(picks):
    parsed = X.Picks(picks=[X.Pick(**p) for p in picks])
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=parsed)
    calls = []

    def parse(**kw):
        calls.append(kw)
        return resp

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse))), calls


def test_rules_mode_without_a_key(master):
    res = X.match_lines([line("waterproofing at toilet floor")], master)
    assert res.mode == "rules" and res.lines[0].decided_by == "rules"


def test_claude_pick_from_shortlist_is_used(master):
    client, calls = fake_client([{"line_id": "1", "code": "WP.01.A", "kind": "exact", "reason": "Same item, spec A."}])
    res = X.match_lines([line("waterproofing at toilet floor")], master, use_llm=True, client=client)
    assert res.mode == "claude" and calls and calls[0]["model"] == extract.MODEL
    m = res.lines[0]
    assert (m.code, m.decided_by, m.reason) == ("WP.01.A", "claude", "Same item, spec A.")


def test_claude_pick_outside_shortlist_falls_back(master):
    client, _ = fake_client([{"line_id": "1", "code": "WP.99", "kind": "exact", "reason": "Made up."}])
    m = X.match_lines([line("waterproofing at toilet floor")], master, use_llm=True, client=client).lines[0]
    assert (m.code, m.decided_by) == ("WP.01", "rules")
    assert any("isn't on the shortlist" in f.text for f in m.flags)


def test_claude_none_gets_a_new_code_suggestion(master):
    client, _ = fake_client([{"line_id": "1", "code": None, "kind": "none", "reason": "Not waterproofing."}])
    m = X.match_lines([line("waterproofing at toilet floor")], master, use_llm=True, client=client).lines[0]
    assert m.kind == "none" and m.code is None and m.suggestion.code not in master.codes()


# ── API ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ic_api.router)
    return TestClient(app)


def check_summary_shape(s):
    assert set(s) == {"title", "headline", "stats", "ready"}
    for st in s["stats"]:
        assert set(st) == {"label", "value", "tone"} and st["tone"] in ("good", "warn", "bad", None)


def test_api_without_master_says_so(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ITEM_MASTER_PATH", str(tmp_path / "missing.xlsx"))
    for url in ("/master", "/duplicates", "/match/1011", "/summary"):
        r = client.get("/api/itemcodes" + url)
        assert r.status_code == 200 and r.json()["ready"] is False, url
    assert client.post("/api/itemcodes/match", json=[{"text": "toilet floor"}]).json()["ready"] is False
    check_summary_shape(client.get("/api/itemcodes/summary").json())


def test_api_with_master(client, xlsx):
    s = client.get("/api/itemcodes/summary").json()
    check_summary_shape(s)
    assert s["ready"] is True
    d = client.get("/api/itemcodes/duplicates").json()
    assert d["counts"]["same_code_different_text"] == 2
    m = client.get("/api/itemcodes/master").json()
    assert m["rows"] == len(ROWS) and "repeated" in m["entries"][6]["issues"]
    r = client.get("/api/itemcodes/match/1010").json()
    assert r["mode"] == "rules" and r["lines"][0]["code"] == "W.P-COBA"
    assert client.get("/api/itemcodes/match/0000").status_code == 404
    p = client.post("/api/itemcodes/match", json=[{"text": "waterproofing at kitchen floor", "site_code": "WP.04"}]).json()
    assert p["lines"][0]["kind"] == "exact" and p["lines"][0]["code"] == "WP.04"
    assert client.post("/api/itemcodes/match", json=[]).status_code == 400
