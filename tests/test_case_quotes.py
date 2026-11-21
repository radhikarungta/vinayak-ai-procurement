"""Steps 6 and 7: quotes per requirement, comparison, summary, award and the HOD email.
Made-up vendors, made-up quote text in fake "PDFs" (the parser is fed the text); no network."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement.cases import api as cases_api
from procurement.cases import flow as F
from procurement.cases import quotes as CQ
from procurement.cases import reply as R
from procurement.cases import rfq as Q
from procurement.quotes import compare as C
from procurement.quotes import pack
from procurement.quotes.models import Quote, QuoteLine, TenderLine
from procurement.quotes.parse import build_quote
from tests.test_cases import setup, vendors  # noqa: F401 - fixtures: made-up inbox, master, vendors

HEAD = "SL NO.   ITEM DESCRIPTION                              UNIT      QUANTITY     RATE       AMOUNT"


def pdf(rows: list[tuple], terms: list[str] = ()) -> bytes:
    """A fake quote PDF: '%PDF' then layout text, read by the patched parser."""
    out = [HEAD]
    for sl, desc, unit, qty, rate in rows:
        figs = f"{qty:>10,.2f}" + (f"{rate:>10,.2f}{qty * rate:>14,.2f}" if rate is not None else "")
        out.append(f"   {sl}     {desc:<40}      {unit:<6}{figs}")
    return ("%PDF-1.4\n" + "\n".join(out + ["Terms", *terms])).encode()


@pytest.fixture(autouse=True)
def fake_parser(monkeypatch):
    def parse(path):
        text = path.read_bytes().decode().split("\n", 1)[1]
        return build_quote(path.stem, path.name, [text], "Scanner App" if "SCAN" in text else "Word")

    monkeypatch.setattr(CQ, "_parse", parse)


def ready(uid="2", ids=("v1", "v2", "v3"), send=True):
    """Approve, pick vendors, and mark every RFQ as sent."""
    F.approve(uid)
    F.pick_vendors(uid, "Waterproofing", list(ids))
    if send:
        for d in Q.drafts(F.build_case(uid)):
            Q.send(uid, d.vendor_id, "x@test.example", "s", "b", category=d.category, mark_only=True)
    return F.build_case(uid)


def up(vid, rows, terms=(), uid="2"):
    return CQ.upload(uid, vid, "Waterproofing", pdf(rows, terms), f"quote-{vid}.pdf")


def cat(uid="2"):
    return CQ.view(F.build_case(uid))["categories"][0]


def test_rfqs_sent_with_no_quotes_waits_and_opens_step_6(vendors):
    c = ready(ids=("v1", "v2"))
    assert c.stage == "rfq_sent" and c.quotes_in == 0 and "upload them in step 6" in c.headline
    v = CQ.view(c)
    assert v["open"] and v["hod_draft"] is None
    g = v["categories"][0]
    assert [w["vendor_id"] for w in g["waiting"]] == ["v1", "v2"] and g["comparison"] is None and g["recommended"] is None
    assert g["basis"]["kind"] == "requirement" and g["basis"]["lines"][0]["code"] == "W.P-COBA"


def test_step_6_is_closed_before_any_rfq_or_quote(vendors):
    c = ready(send=False)
    assert c.stage == "rfq" and CQ.view(c)["open"] is False


def test_upload_compares_on_the_requirement_quantities_with_unit_conversion(vendors):
    ready()
    with pytest.raises(F.CaseError, match="isn't a PDF"):
        CQ.upload("2", "v1", "Waterproofing", b"hello", "x.txt")
    with pytest.raises(F.CaseError, match="isn't picked"):
        CQ.upload("2", "zz", "Waterproofing", pdf([]), "x.pdf")
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    c = up("v2", [(1, "BRICK BAT COBA ON ROOF", "SQFT", 3229.17, 3.9)])
    assert c.stage == "quotes" and c.quotes_in == 2 and (F.cases_dir() / "2.quotes").is_dir()
    g = cat()
    assert [q["source"] for q in g["quotes"]] == ["upload", "upload"] and g["quotes"][0]["file"] == "quote-v1.pdf"
    assert g["selected"] == ["v1", "v2"] and [w["vendor_id"] for w in g["waiting"]] == ["v3"]
    vs = {v["vendor"]: v for v in g["comparison"]["vendors"]}
    assert vs["Vendor 1"]["repriced_total"] == pytest.approx(12000)
    assert vs["Vendor 2"]["repriced_total"] == pytest.approx(3.9 * 10.7639 * 300, rel=1e-4)  # per sqft -> per sqm
    assert g["comparison"]["lowest"] == {1: ["v1"]}
    assert g["recommended"]["vendor_id"] == "v1" and g["recommended"]["like_for_like"]
    assert any(s.startswith("Lowest on the same quantities: Vendor 1 at ₹12,000") for s in g["summary"])
    assert g["summary"][0].startswith("Compared on this requirement's approved quantities")


def test_trap_terms_and_different_quantities_in_the_summary(vendors):
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)], ["1. GST @ 18% as applicable.", "2. Validity of our offer: 30 days."])
    up("v2", [(1, "BRICK BAT COBA ON ROOF", "SQM", 100, 45)], ["1. GST extra."])
    g = cat()
    s = " ".join(g["summary"])
    assert "Trap: Vendor 2 looks cheapest as quoted (₹4,500, its own PDF total), but on the same quantities it is 12% above the lowest." in s
    assert "Vendor 2 priced a different quantity set" in s
    assert "GST differs: 18% extra (Vendor 1); extra, rate not stated (Vendor 2)." in s
    assert "Validity differs: 30 days (Vendor 1); not stated (Vendor 2)." in s
    assert g["recommended"]["vendor_id"] == "v1"


def test_needs_vision_quote_is_left_out_until_rates_are_typed_in(vendors):
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    up("v3", [(1, "BRICK BAT COBA ON ROOF SCAN", "SQM", 300, None)])
    g = cat()
    v3 = next(q for q in g["quotes"] if q["vendor_id"] == "v3")
    assert v3["quote"]["needs_vision"] and not v3["selected"] and g["selected"] == ["v1"]
    assert any("Vendor 3's quote is a scan without readable rates: it needs a person to read the rates" in x for x in g["summary"])
    with pytest.raises(F.CaseError, match="Not a line"):
        CQ.set_rates("2", "v3", "Waterproofing", {"ZZ": 1})
    CQ.set_rates("2", "v3", "Waterproofing", {"W.P-COBA": 38})
    g = cat()
    v3 = next(q for q in g["quotes"] if q["vendor_id"] == "v3")
    assert not v3["quote"]["needs_vision"] and v3["selected"] and v3["manual_rates"] == {"W.P-COBA": 38}
    assert "typed in by a person" in " ".join(v3["quote"]["notes"])
    assert g["recommended"]["vendor_id"] == "v3" and "₹11,400" in g["recommended"]["reason"]


def test_incomplete_quote_and_no_quantity_line_on_a_two_code_basis(vendors):
    F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"])
    F.decide("1", "1-r5", "skip")
    F.decide("1", "1-r6", "skip")
    ready("1", ids=("v1", "v2"))
    up("v1", [(1, "WP.02 TOILET WALL", "SQM", 80, 50), (2, "TOILET FLOOR", "SQM", 120, 40)], uid="1")  # matched by code text, then RFQ order
    up("v2", [(1, "TOILET FLOOR", "SQM", 120, 42)], uid="1")
    g = cat("1")
    assert [t["code"] for t in g["basis"]["lines"]] == ["WP.01", "WP.02"]
    assert [t["qty"] for t in g["basis"]["lines"]] == [120, None]  # WP.02 shares the site's total with WP.01
    v1 = next(v for v in g["comparison"]["vendors"] if v["vendor"] == "Vendor 1")
    assert [ln["rate"] for ln in v1["lines"]] == [40, 50]
    s = " ".join(g["summary"])
    assert "No quantity in the requirement for WP.02" in s
    assert "Vendor 2's quote is incomplete (no comparable rate for WP.02)" in s
    assert g["recommended"]["vendor_id"] == "v1"


@pytest.fixture
def tender_folder(tmp_path, monkeypatch):
    """Two made-up vendors' quotes in a tender folder, priced on a tender with an unmeasured line."""
    d = tmp_path / "pack" / "06_quotations"
    d.mkdir(parents=True)
    rows = {"Vendor_1": [(1, "WATERPROOFING AT TOILET FLOOR", "SQFT", 1000, 40), (2, "WATERPROOFING ON DEPRESSION", "RMT", 50, 90)],
            "Vendor_2": [(1, "WATERPROOFING AT TOILET FLOOR", "SQFT", 2500, 35), (2, "WATERPROOFING ON DEPRESSION", "SQM", 80, 60)]}
    for name, r in rows.items():
        (d / f"Quotation_{name}.pdf").write_bytes(pdf(r))
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path / "pack"))
    tender = [TenderLine(sl_no=1, code="WP.01", description="WATERPROOFING AT TOILET FLOOR", unit="sqft", qty=1000.0),
              TenderLine(sl_no=2, code="WP.03", description="WATERPROOFING ON TOILET DEPRESSION", unit="rmt", qty=50.0, measured=False, note="Not measured")]
    monkeypatch.setattr(pack, "tender", lambda: [t.model_copy() for t in tender])
    return tender


def test_tender_folder_quotes_link_by_vendor_name_and_compare_on_the_tender(vendors, tender_folder):
    c = ready()
    assert c.stage == "quotes" and c.quotes_in == 2  # same kind of work: "waterproofing"
    g = cat()
    assert [(q["vendor_id"], q["source"], q["linked"]) for q in g["quotes"]] == [("v1", "pack", "auto"), ("v2", "pack", "auto")]
    assert g["basis"]["kind"] == "tender" and [w["vendor_id"] for w in g["waiting"]] == ["v3"]
    s = " ".join(g["summary"])
    assert "priced on the tender BOQ in the tender folder, not on this requirement's item code (W.P-COBA)" in s
    assert "WP.03 (waterproofing on toilet depression) is not comparable: not measured on the site sheet" in s
    assert "Trap: Vendor 2 looks cheapest" not in s and "Lowest on the same quantities: Vendor 2 at ₹35,000" in s
    # A person can stop using one, and use it again; an upload wins over the tender folder.
    CQ.link("2", "v1", "Waterproofing", False)
    g = cat()
    assert [q["vendor_id"] for q in g["quotes"]] == ["v2"] and [o["vendor_id"] for o in g["offers"]] == ["v1"]
    CQ.link("2", "v1", "Waterproofing", True)
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    assert {q["vendor_id"]: q["source"] for q in cat()["quotes"]} == {"v1": "upload", "v2": "pack"}
    with pytest.raises(F.CaseError, match="no quote from this vendor in the tender folder"):
        CQ.link("2", "v3", "Waterproofing", True)


def test_tender_folder_quotes_for_other_work_are_offered_not_linked(vendors, tender_folder, monkeypatch):
    monkeypatch.setattr(pack, "tender", lambda: [TenderLine(sl_no=1, code="SC.01", description="CUPLOCK SCAFFOLDING HIRE", unit="sqft", qty=10.0)])
    c = ready()
    assert c.stage == "rfq_sent" and c.quotes_in == 0
    assert [o["vendor_id"] for o in cat()["offers"]] == ["v1", "v2"]


def test_award_recommended_or_override_with_a_reason_then_hod(vendors, monkeypatch):
    monkeypatch.setenv("HOD_EMAIL", "hod@test.example")
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)], ["1. GST @ 18% as applicable."])
    up("v2", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 44)])
    with pytest.raises(R.SendError, match="Choose the vendor"):
        CQ.send_hod("2", "hod@test.example", "s", "b", mark_only=True)
    with pytest.raises(F.CaseError, match="Say why"):
        CQ.award("2", "Waterproofing", "v2")
    with pytest.raises(F.CaseError, match="Tick that vendor"):
        CQ.award("2", "Waterproofing", "v3")
    c = CQ.award("2", "Waterproofing", "v1")
    assert c.stage == "hod" and c.award["Waterproofing"]["vendor"] == "Vendor 1" and not c.award["Waterproofing"]["override"]
    assert "Next: email the HOD" in c.headline
    c = CQ.award("2", "Waterproofing", "v2", "Faster start on site.")
    assert c.award["Waterproofing"]["override"] and c.award["Waterproofing"]["reason"] == "Faster start on site."
    d = CQ.view(c)["hod_draft"]
    assert d["to"] == "hod@test.example" and d["subject"] == "For approval: Vendor 2 for waterproofing at Riverside"
    b = d["body"]
    assert "Recommended: Vendor 2. Faster start on site." in b
    assert "(Lowest on the comparison was Vendor 1: Lowest complete quote on the same quantities: ₹12,000 excl. GST." in b
    assert "Vendor 1" in b and "₹13,200" in b and "GST 18% extra" in b  # the plain-text table
    assert "Please reply to approve, or with any changes" in b
    with pytest.raises(R.SendError) as e:
        CQ.send_hod("2", "hod@test.example", d["subject"], b)
    assert e.value.status == 403  # not a test inbox
    CQ.send_hod("2", "hod@test.example", d["subject"], b, mark_only=True)
    c = F.build_case("2")
    assert c.stage == "hod" and c.hod["via"] == "marked" and "awaiting approval" in c.headline
    c = CQ.approve_hod("2")
    assert c.stage == "done" and c.hod["approved_by_hod_at"] and c.headline == "HOD approved Vendor 2."


def test_hod_send_goes_only_to_test_inboxes(vendors, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): ...
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setenv("REPLY_ALLOWED_TO", "@test.example")
    monkeypatch.setenv("GMAIL_ADDRESS", "desk@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    CQ.award("2", "Waterproofing", "v1")
    with pytest.raises(R.SendError):
        CQ.send_hod("2", "hod@company.example", "s", "b", smtp_factory=FakeSMTP)
    h = CQ.send_hod("2", "hod@test.example", "s", "b", smtp_factory=FakeSMTP)
    assert h["via"] == "smtp" and sent[0]["To"] == "hod@test.example"
    with pytest.raises(F.CaseError):
        CQ.approve_hod("1")  # nothing sent on that one


def test_changes_drop_the_award_and_the_hod_email(vendors):
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    up("v2", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 44)])
    CQ.award("2", "Waterproofing", "v1")
    CQ.send_hod("2", "hod@test.example", "s", "b", mark_only=True)
    c = CQ.pick_compare("2", "Waterproofing", ["v2"])  # the chosen vendor left out
    assert c.stage == "quotes" and not c.award and c.hod is None
    assert F.load_state("2")["hod_history"][0]["to"] == "hod@test.example"
    assert cat()["selected"] == ["v2"] and cat()["recommended"]["vendor_id"] == "v2"
    with pytest.raises(F.CaseError, match="No quote in from"):
        CQ.pick_compare("2", "Waterproofing", ["v3"])
    CQ.award("2", "Waterproofing", "v2")
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 39)])  # a new quote: choose again
    c = F.build_case("2")
    assert c.stage == "quotes" and not c.award and sorted(cat()["selected"]) == ["v1", "v2"]
    CQ.award("2", "Waterproofing", "v1")
    c = F.pick_vendors("2", "Waterproofing", ["v1", "v2"])  # picks changed
    assert not c.award and "Waterproofing" not in (F.load_state("2").get("compare") or {})
    c = F.approve("2", False)  # withdrawing the approval drops the quotes with the RFQs
    st = F.load_state("2")
    assert c.stage == "approve" and not any(k in st for k in ("quote_uploads", "compare", "award", "hod"))
    assert F.approve("2").quotes_in == 0


def test_api_round_trip(vendors, monkeypatch):
    monkeypatch.setenv("HOD_EMAIL", "hod@test.example")
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    ready()
    u = "/api/cases/2"
    r = cl.post(f"{u}/quotes/v1?category=Waterproofing", files={"file": ("q1.pdf", pdf([(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)]), "application/pdf")})
    assert r.status_code == 200 and r.json()["stage"] == "quotes" and r.json()["quotes"]["categories"][0]["quotes"][0]["file"] == "q1.pdf"
    assert cl.post(f"{u}/quotes/v2?category=Waterproofing", files={"file": ("x.txt", b"nope", "text/plain")}).status_code == 400
    cl.post(f"{u}/quotes/v2?category=Waterproofing", files={"file": ("q2.pdf", pdf([(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 50)]), "application/pdf")})
    assert cl.get(f"{u}/quotes").json()["categories"][0]["recommended"]["vendor_id"] == "v1"
    assert cl.post(f"{u}/compare", json={"category": "Waterproofing", "vendor_ids": ["v1", "v2"]}).status_code == 200
    assert cl.post(f"{u}/award", json={"category": "Waterproofing", "vendor_id": "v2"}).status_code == 400
    c = cl.post(f"{u}/award", json={"category": "Waterproofing", "vendor_id": "v1"}).json()
    assert c["stage"] == "hod" and c["quotes"]["hod_draft"]["to"] == "hod@test.example"
    assert cl.post(f"{u}/hod/approve", json={}).status_code == 400
    c = cl.post(f"{u}/hod", json={"to": "hod@test.example", "subject": "s", "body": "b", "mark_only": True}).json()
    assert c["hod"]["via"] == "marked" and c["quotes"]["hod_draft"]["sent"]["to"] == "hod@test.example"
    assert cl.post(f"{u}/hod/approve", json={}).json()["stage"] == "done"
    item = next(i for i in cl.get("/api/cases").json()["items"] if i["uid"] == "2")
    assert item["awarded"] == ["Vendor 1"] and item["hod_sent"] and item["quotes_in"] == 2
    s = cl.get("/api/cases/summary").json()
    assert {x["label"]: x["value"] for x in s["stats"]}["Approved"] == 1


def test_a_lowest_total_of_zero_does_not_break_the_summary_or_the_recommendation(vendors):
    basis = [TenderLine(sl_no=1, code="W.P-COBA", description="COBA", unit="sqm", qty=300.0)]
    qs = [Quote(vendor=n, file=f"{n}.pdf", read_confidence="high",
                lines=[QuoteLine(sl_no=1, description="COBA", unit="sqm", unit_as_written="SQM", qty=300.0, rate=r, amount=None, page=1)])
          for n, r in (("Vendor 1", 0.0), ("Vendor 2", 40.0))]
    cmp = C.compare(basis, qs)
    assert [v.pct_above_lowest for v in cmp.vendors] == [None, None]
    cmp.vendors[0].rank_as_quoted, cmp.vendors[1].rank_as_quoted = 2, 1  # Vendor 2 "cheapest as quoted": the trap line
    s = " ".join(CQ.summary("requirement", basis, ["W.P-COBA"], cmp, qs, qs, []))
    assert "Then Vendor 2 (₹12,000)." in s and "on the same quantities it is not the lowest." in s
    assert CQ.recommend(cmp, ["v1", "v2"])["reason"].endswith("The next, Vendor 2, is at ₹12,000.")
    ready()
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    with pytest.raises(F.CaseError, match="more than 0"):
        CQ.set_rates("2", "v1", "Waterproofing", {"W.P-COBA": 0})


def test_lines_sharing_a_code_add_up_their_quantities(vendors):
    ln = F.build_case("2").lines[0]

    def mk(i, qty, uom, codes=("W.P-COBA",)):
        return ln.model_copy(update={"id": f"x-{i}", "text": f"line {i}", "quantity": qty, "uom": uom, "codes": list(codes)})

    b = CQ.case_basis([mk(1, 100, "SQM"), mk(2, 50, "SQM")], {})
    assert [(t.code, t.qty, t.unit) for t in b] == [("W.P-COBA", 150, "sqm")]
    b = CQ.case_basis([mk(1, 100, "SQM"), mk(2, 1076.39, "SQFT")], {})
    assert b[0].qty == pytest.approx(200, rel=1e-4) and b[0].unit == "sqm"
    b = CQ.case_basis([mk(1, 100, "SQM"), mk(2, 4, "NOS")], {})
    assert b[0].qty is None and "SQM, NOS" in b[0].note
    s = " ".join(CQ.summary("requirement", b, ["W.P-COBA"], None, [], [], []))
    assert "No single quantity for W.P-COBA: the requirement gives it in SQM, NOS" in s and "No quantity in the requirement" not in s
    b = CQ.case_basis([mk(1, 100, "SQM", ("WP.01", "W.P-COBA")), mk(2, 50, "SQM")], {})
    assert [(t.code, t.qty) for t in b] == [("WP.01", None), ("W.P-COBA", 50)]  # a shared total adds nothing


def test_changing_the_compared_set_drops_the_award_even_if_the_vendor_stays(vendors):
    ready()
    for vid, rate in (("v1", 40), ("v2", 44), ("v3", 46)):
        up(vid, [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, rate)])
    CQ.award("2", "Waterproofing", "v1")
    CQ.send_hod("2", "hod@test.example", "s", "b", mark_only=True)
    c = CQ.pick_compare("2", "Waterproofing", ["v3", "v2", "v1"])  # same set: nothing changes
    assert c.award and c.hod
    c = CQ.pick_compare("2", "Waterproofing", ["v1", "v2"])  # v3 left out, v1 still in
    assert c.stage == "quotes" and not c.award and c.hod is None


def test_a_code_named_on_two_rows_goes_to_the_first_row_only(vendors):
    ready()
    up("v1", [(1, "W.P-COBA BRICK BAT COBA ON ROOF", "SQM", 300, 40), (2, "W.P-COBA EXTRA OVER FOR PARAPET", "SQM", 20, 99)])
    q = cat()["quotes"][0]["quote"]
    assert [ln["sl_no"] for ln in q["lines"]] == [1, 1002]
    v1 = cat()["comparison"]["vendors"][0]
    assert v1["lines"][0]["rate"] == 40 and v1["repriced_total"] == pytest.approx(12000)


def test_typed_rates_clear_a_scan_even_with_an_unmapped_row(vendors):
    ready()
    up("v3", [(1, "BRICK BAT COBA ON ROOF SCAN", "SQM", 300, None), (2, "SCAFFOLD HIRE", "SQM", 10, None)])
    assert cat()["quotes"][0]["quote"]["needs_vision"]
    CQ.set_rates("2", "v3", "Waterproofing", {"W.P-COBA": 38})
    q = cat()["quotes"][0]
    assert not q["quote"]["needs_vision"] and q["selected"] and [ln["sl_no"] for ln in q["quote"]["lines"]] == [1, 1002]


def test_upload_names_differ_per_exact_category_and_old_uploads_still_read(vendors):
    a, b = CQ.upload_path("2", "v1", "Civil & Masonry"), CQ.upload_path("2", "v1", "Civil Masonry")
    assert a != b and a.name.startswith("v1.civil-masonry.") and b.name.startswith("v1.civil-masonry.")
    ready()
    old = CQ.quotes_dir("2") / "v1.waterproofing.pdf"  # kept before the hash was in the name
    old.parent.mkdir(parents=True)
    old.write_bytes(pdf([(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)]))
    st = F.load_state("2")
    st["quote_uploads"] = {"v1|Waterproofing": {"file": old.name, "original": "q.pdf", "at": "2026-11-20"}}
    F.save_state("2", st)
    g = cat()
    assert [(q["vendor_id"], q["file"]) for q in g["quotes"]] == [("v1", "q.pdf")]
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 39)])  # a new upload goes to the new name and wins
    assert CQ.upload_path("2", "v1", "Waterproofing", stored=True) != old and cat()["recommended"]["reason"].startswith(
        "Lowest complete quote on the same quantities: ₹11,700")


def test_no_award_or_hod_stage_while_an_rfq_is_unsent(vendors):
    ready(send=False)
    for d in Q.drafts(F.build_case("2")):
        if d.vendor_id != "v3":
            Q.send("2", d.vendor_id, "x@test.example", "s", "b", category=d.category, mark_only=True)
    up("v1", [(1, "BRICK BAT COBA ON ROOF", "SQM", 300, 40)])
    with pytest.raises(F.CaseError, match="Send or mark as sent every RFQ"):
        CQ.award("2", "Waterproofing", "v1")
    st = F.load_state("2")
    st["award"] = {"Waterproofing": {"vendor_id": "v1", "vendor": "Vendor 1", "reason": "r", "at": F.now()}}  # an award from before
    F.save_state("2", st)
    assert F.build_case("2").stage == "rfq"
