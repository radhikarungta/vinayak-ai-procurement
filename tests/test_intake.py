from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from procurement import api
from procurement.intake import extract
from procurement.intake.checks import run_checks
from procurement.intake.email_parser import parse_text, split_thread
from procurement.intake.models import Cited, ExtractedRequirement, ThreadPosition
from procurement.intake.pipeline import load_email, run_intake
from procurement.intake.quantity_sheet import parse_quantity_sheet

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EML = SAMPLES / "riverside_t2_waterproofing.eml"
REPLAY = SAMPLES / "riverside_t2_waterproofing.extraction.json"


@pytest.fixture
def mail():
    return load_email(EML.read_bytes())


@pytest.fixture
def reference() -> ExtractedRequirement:
    return extract.extract_replay(REPLAY)


# ── email parsing ──────────────────────────────────────────────────────────────────────────────


def test_eml_headers_body_and_attachment(mail):
    assert mail.subject.startswith("Re: WO Requirement of Waterproofing")
    assert "riverside.site@example.com" in mail.sender
    assert [a.filename for a in mail.attachments] == ["T2_waterproofing_quantities.xlsx"]
    assert mail.body.startswith("Dear Sir,")


def test_thread_splits_into_three_messages(mail):
    thread = split_thread(mail.body)
    assert len(thread) == 3
    assert thread[1].header.startswith("On Thu, Nov 12, 2026")
    assert thread[2].header.startswith("From: Vinayak Riverside")
    assert "issue the Work Order" in thread[2].body
    assert not any(line.startswith(">") for t in thread for line in t.body.splitlines())


def test_pasted_text_with_pseudo_headers():
    m = parse_text("SUBJECT: Need cement\nFROM: Site\nDATE: 2026-11-20\n------------------------------\n\nSend 200 bags.")
    assert (m.subject, m.sender, m.date) == ("Need cement", "Site", "2026-11-20")
    assert m.body == "Send 200 bags."


# ── quantity sheet ─────────────────────────────────────────────────────────────────────────────


def test_quantity_sheet_lines_and_totals(mail):
    a = mail.attachments[0]
    sheet = parse_quantity_sheet(a.data, a.filename)
    assert sheet is not None
    assert [ln.code for ln in sheet.lines] == [f"WP.0{i}" for i in range(1, 9)]
    assert [ln.code for ln in sheet.lines if not ln.measured] == ["WP.03", "WP.08"]
    assert sheet.breakdown_columns[0] == "FLAT - A"
    assert sheet.grand_total_sqm == pytest.approx(3484.18, abs=0.01)


# ── quote verification ─────────────────────────────────────────────────────────────────────────


def test_reference_extraction_survives_verification(mail, reference):
    cleaned, dropped = extract.verify_quotes(reference, mail)
    assert dropped == []
    assert cleaned.vendor_named.value == "Nirmal"
    assert len(cleaned.thread) == 3


def test_invented_quote_is_dropped(mail, reference):
    bad = reference.model_copy(deep=True)
    bad.finish = Cited(value="March 2028", quote="completed by March 2028")
    bad.thread.append(ThreadPosition(who="MD", position="Approves Nirmal", quote="I approve Nirmal for all floors"))
    cleaned, dropped = extract.verify_quotes(bad, mail)
    assert cleaned.finish.value is None
    assert {d.field for d in dropped} == {"finish", "thread[3]"}


def test_quote_spanning_quoted_line_break_is_found(mail):
    hay = extract._norm(extract.email_as_prompt(mail))
    assert extract.quote_found("QS & Billing Engineer Vinayak Riverside", hay)
    assert extract.quote_found("the MD advised engaging Nirmal ... for Tower 2", hay)
    assert not extract.quote_found("", hay)


# ── live extraction, with a stand-in client ────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self._response = response

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_live_extraction_request_shape(mail, reference):
    client = FakeClient(SimpleNamespace(stop_reason="end_turn", parsed_output=reference))
    result = run_intake(mail, client=client)
    call = client.calls[0]
    assert call["model"] == extract.MODEL
    assert call["output_format"] is ExtractedRequirement
    assert call["fallbacks"] == "default"
    assert "T2_waterproofing_quantities.xlsx" in call["messages"][0]["content"]
    assert result.extraction_mode == "live"


def test_refusal_raises(mail):
    client = FakeClient(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    with pytest.raises(extract.IntakeError, match="declined"):
        run_intake(mail, client=client)


# ── checks ─────────────────────────────────────────────────────────────────────────────────────


def test_sample_checks(mail):
    result = run_intake(mail, replay=REPLAY)
    by_id = {c.id: c for c in result.checks}
    assert by_id["vendor-before-tender"].severity == "block"
    assert by_id["unmeasured-lines"].severity == "warn"
    assert by_id["sqft-factor"].severity == "warn" and "10.76" in by_id["sqft-factor"].detail
    assert by_id["grand-total"].severity == "ok"
    assert result.checks[0].severity == "block"


def test_missing_sheet_is_blocking(mail, reference):
    mail.attachments.clear()
    checks = run_checks(mail, reference, None)
    assert any(c.id == "attachment-missing" and c.severity == "block" for c in checks)


# ── API ────────────────────────────────────────────────────────────────────────────────────────


def test_api_sample_in_replay(monkeypatch):
    monkeypatch.setenv("INTAKE_REPLAY", "1")
    r = TestClient(api.app).get("/api/samples/riverside")
    assert r.status_code == 200
    body = r.json()
    assert body["extraction_mode"] == "replay"
    assert body["quantities"]["grand_total_sqm"] == pytest.approx(3484.18, abs=0.01)


def test_api_upload_without_key_says_why(monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    r = TestClient(api.app).post("/api/intake/email", data={"text": "SUBJECT: x\n\nNeed cement"})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_api_rejects_empty_request():
    r = TestClient(api.app).post("/api/intake/email", data={"text": "  "})
    assert r.status_code == 400
