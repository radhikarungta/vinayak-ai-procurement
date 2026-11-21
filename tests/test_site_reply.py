"""The site's answer to our reply: linked back to its case (not a new requirement), read into suggestions
a person accepts. Same made-up master and emails as test_cases; no network."""

from __future__ import annotations

import json
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement import mailbox
from procurement.cases import api as cases_api
from procurement.cases import flow as F
from procurement.cases import reply as R
from procurement.cases import site_reply as SR
from tests.test_cases import line, setup  # noqa: F401 - autouse fixture: temp master, demo inbox, cases dir

REPLY = """Hi,

1. Toilets: it's both, floor 80 sqm and wall 40 sqm.
2. Drop the cement bags, we'll buy them locally.
3. WP.MR on our sheet is a typo, it's the toilet floor.

Thanks

On Mon, 16 Nov 2026 at 10:00, Procurement <desk@test.example> wrote:
> Please confirm these points:
> 2. OPC 53 cement bags - we have no item code for this
"""


def answers(*items):
    return SR.Answers(answers=[SR.Answer(**({"codes": [], "quote": "", "note": ""} | a)) for a in items])


def fake_client(parsed):
    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(status="completed", output=[], output_parsed=parsed)

    return SimpleNamespace(responses=SimpleNamespace(parse=parse)), calls


GOOD = answers(
    {"ref": "1-r4", "action": "use_code", "codes": ["WP.01", "WP.02"], "quote": "it's both, floor 80 sqm and wall 40 sqm", "note": "floor 80, wall 40"},
    {"ref": "1-r5", "action": "skip", "quote": "Drop the cement bags"},
    {"ref": "1-r6", "action": "use_code", "codes": ["WP.01"], "quote": "WP.MR on our sheet is a typo, it's the toilet floor"},
    {"ref": "check:basics", "action": "unclear"},
)


class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setenv("REPLY_ALLOWED_TO", "@example.com")
    monkeypatch.setenv("GMAIL_ADDRESS", "desk@test.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd")
    return FakeSMTP


# ── threading ──────────────────────────────────────────────────────────────────────────────────


def test_sent_reply_carries_a_message_id_kept_on_the_case(smtp):
    r = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    assert r.message_id and r.message_id.endswith("@test.example>")
    assert smtp.sent[0]["Message-ID"] == r.message_id
    assert F.build_case("1").replies[0].message_id == r.message_id
    marked = R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    assert marked.message_id is None  # never left our outbox: nothing can reply to it


def test_match_by_in_reply_to_then_by_subject_and_sender(smtp):
    r = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    assert SR.match_case(in_reply_to=r.message_id, references=None, subject="anything", sender="x@y.com") == "1"
    assert SR.match_case(in_reply_to=None, references=f"<a@b> {r.message_id}", subject=None, sender=None) == "1"
    # No headers (some phone clients drop them): same subject AND the address we wrote to.
    assert SR.match_case(in_reply_to=None, references=None, subject="RE: Re: Waterproofing, Tower-1", sender="Site <SITE@example.com>") == "1"
    assert SR.match_case(in_reply_to=None, references=None, subject="Re: Waterproofing, Tower-1", sender="other@example.com") is None
    assert SR.match_case(in_reply_to=None, references=None, subject="Waterproofing, Tower-9", sender="site@example.com") is None


def test_new_text_drops_the_quoted_mail():
    t = SR.new_text(REPLY)
    assert "floor 80 sqm" in t and "wrote:" not in t and "we have no item code" not in t


# ── reading the reply ──────────────────────────────────────────────────────────────────────────


def test_reply_read_into_checked_suggestions():
    client, calls = fake_client(GOOD)
    r = SR.record("1", REPLY, sender="site@example.com", client=client)
    assert r.read == "read"
    by = {s.ref: s for s in r.suggestions}
    assert list(by) == ["1-r4", "1-r5", "1-r6", "check:basics"]  # every question, in order
    assert by["1-r4"].action == "use_code" and by["1-r4"].codes == ["WP.01", "WP.02"]
    assert by["1-r5"].action == "skip" and by["check:basics"].action == "unclear"
    prompt = calls[0]["input"][0]["content"]
    assert "ref: 1-r4" in prompt and "ref: check:basics" in prompt and "wrote:" not in prompt
    assert "1-r3" not in prompt  # a settled line isn't asked about


def test_made_up_quote_or_code_becomes_unclear():
    bad = answers(
        {"ref": "1-r4", "action": "use_code", "codes": ["WP.01"], "quote": "floor only please"},  # not in the reply
        {"ref": "1-r5", "action": "use_code", "codes": ["CEM.99"], "quote": "Drop the cement bags"},  # invented code
        {"ref": "1-r6", "action": "resolved", "quote": "it's the toilet floor"},  # "sorted" on a line
        {"ref": "check:basics", "action": "skip", "quote": "Thanks"},  # skip on a check
    )
    r = SR.record("1", REPLY, client=fake_client(bad)[0])
    assert [s.action for s in r.suggestions] == ["unclear"] * 4
    assert "aren't in the reply" in r.suggestions[0].why_unclear
    assert "CEM.99" in r.suggestions[1].why_unclear
    assert all(s.codes == [] for s in r.suggestions)


def test_without_a_key_it_is_kept_and_read_later():
    r = SR.record("1", REPLY)
    assert r.read == "needs_key" and r.suggestions == []
    r = SR.reread("1", 0, client=fake_client(GOOD)[0])
    assert r.read == "read" and len(r.suggestions) == 4
    assert F.load_state("1")["site_replies"][0]["read"] == "read"


def test_empty_reply_is_refused():
    with pytest.raises(F.CaseError):
        SR.record("1", "> only the quoted mail\n")


# ── the case moves ─────────────────────────────────────────────────────────────────────────────


def test_answer_flags_the_case_and_accepting_settles_it():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    assert F.build_case("1").stage == "waiting_on_site"
    SR.record("1", REPLY, client=fake_client(GOOD)[0])
    c = F.build_case("1")
    assert c.stage == "item_codes" and c.site_replied and "site replied" in c.headline
    # Accepting a suggestion is an ordinary decision (the UI posts it to /lines/<id>).
    F.decide("1", "1-r4", "use_code", ["WP.01", "WP.02"], note="floor 80, wall 40")
    F.decide("1", "1-r5", "skip")
    c = F.decide("1", "1-r6", "use_code", ["WP.01"])
    assert line(c, 4).decision.note == "floor 80, wall 40"
    assert c.stage == "approve" and not c.site_replied  # nothing open: the answer needs no more review


# ── mailbox: a reply is not a new requirement ──────────────────────────────────────────────────


def _eml(subject, sender, body, in_reply_to=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "desk@test.example", subject, "<site-1@example.com>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


class Imap:
    def __init__(self, raw):
        self.raw = raw

    def __call__(self, host):
        return self

    def login(self, *a):
        pass

    def select(self, *a, **kw):
        return "OK", [b"1"]

    def uid(self, cmd, *a):
        return ("OK", [b"77"]) if cmd == "SEARCH" else ("OK", [(b"77 (BODY[] {1}", self.raw), b")"])

    def logout(self):
        pass


def test_sync_attaches_the_answer_to_its_case(smtp, monkeypatch):
    sent = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    monkeypatch.setattr(mailbox, "run_intake", lambda *a, **kw: pytest.fail("a site answer must not go through intake"))
    cfg = mailbox.MailConfig(address="desk@test.example", password="x", watch_from=[])
    raw = _eml("Re: Waterproofing, Tower-1", "Site <site@example.com>", REPLY, in_reply_to=sent.message_id)
    out = mailbox.sync(cfg, imap_factory=Imap(raw), client=fake_client(GOOD)[0])
    assert out["site_replies"] == 1 and out["read"] == 0 and out["waiting_for_key"] == 0
    saved = json.loads((mailbox.STORE / "77.json").read_text())
    assert saved["status"] == "site_reply" and saved["case"] == "1"
    assert [i["uid"] for i in mailbox.list_inbox()] == ["77"] and mailbox.list_inbox()[0]["case"] == "1"
    c = F.build_case("1")
    assert c.site_replied and c.site_replies[0]["via"] == "email" and c.site_replies[0]["message_id"] == "<site-1@example.com>"
    assert "m77" not in {str(r["uid"]) for r in F.records()}  # not on the overview as a new requirement


def test_sync_without_a_match_is_a_new_requirement(monkeypatch):
    cfg = mailbox.MailConfig(address="desk@test.example", password="x", watch_from=[])
    raw = _eml("Waterproofing, Tower-9", "Site <site@example.com>", "Need waterproofing for Tower-9.")
    out = mailbox.sync(cfg, imap_factory=Imap(raw))
    assert out["site_replies"] == 0 and out["waiting_for_key"] == 1


# ── API ────────────────────────────────────────────────────────────────────────────────────────


def test_api_paste_and_reread(monkeypatch):
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    c = cl.post("/api/cases/1/site-reply", json={"body": REPLY}).json()
    assert c["site_replies"][0]["read"] == "needs_key" and c["site_replied"]
    monkeypatch.setattr(SR.llm, "ready", lambda: True)
    monkeypatch.setattr(SR.llm, "client", lambda: fake_client(GOOD)[0])
    c = cl.post("/api/cases/1/site-reply/0/read").json()
    assert c["site_replies"][0]["read"] == "read" and len(c["site_replies"][0]["suggestions"]) == 4
    assert cl.post("/api/cases/1/site-reply/5/read").status_code == 400
    assert cl.post("/api/cases/1/site-reply", json={"body": "  "}).status_code == 400


# ── review fixes ───────────────────────────────────────────────────────────────────────────────


def test_subject_fallback_needs_re_skips_our_own_mail_and_picks_the_latest(smtp, monkeypatch):
    monkeypatch.setattr(F, "now", lambda: "2026-11-20T10:00:00+00:00")
    R.send("2", "site@example.com", "Re: Weekly requirement", "b", mark_only=True)
    monkeypatch.setattr(F, "now", lambda: "2026-11-19T10:00:00+00:00")
    R.send("1", "site@example.com", "Re: Weekly requirement", "a", mark_only=True)
    # Next week's fresh email with the original subject is a new requirement, not an answer.
    assert SR.match_case(in_reply_to=None, references=None, subject="Weekly requirement", sender="site@example.com") is None
    # Two cases fit: the one we wrote to last.
    assert SR.match_case(in_reply_to=None, references=None, subject="Re: Weekly requirement", sender="site@example.com") == "2"
    # Our own reply landing in a test inbox we also read.
    monkeypatch.setenv("REPLY_ALLOWED_TO", "@example.com,desk@test.example")
    sent = R.send("3", "desk@test.example", "Re: Toilet floor, Tower-3", "c", smtp_factory=smtp)
    assert SR.match_case(in_reply_to=None, references=None, subject="Re: Toilet floor, Tower-3", sender="desk@test.example") is None
    assert SR.match_case(in_reply_to=None, references=None, subject="x", sender="a@b.c", message_id=sent.message_id) is None


def test_inline_answers_under_the_quoted_questions_are_kept():
    body = ("On Mon, 16 Nov 2026, Procurement wrote:\n> 1. Toilets: floor, wall or both?\nBoth, floor 80 wall 40\n"
            "> 2. OPC 53 cement bags?\nDrop it\n")
    t = SR.new_text(body)
    assert "Both, floor 80 wall 40" in t and "> 1. Toilets" in t and "Drop it" in t
    assert SR.own_words(t) == "Both, floor 80 wall 40\nDrop it"
    # A quote taken from our own question doesn't count as the site's words.
    r = SR.record("1", body, client=fake_client(answers(
        {"ref": "1-r4", "action": "use_code", "codes": ["WP.01", "WP.02"], "quote": "Both, floor 80 wall 40"},
        {"ref": "1-r5", "action": "skip", "quote": "OPC 53 cement bags"},
    ))[0])
    assert [s.action for s in r.suggestions[:2]] == ["use_code", "unclear"]


def test_code_in_other_case_is_mapped_to_the_list():
    r = SR.record("1", REPLY, client=fake_client(answers(
        {"ref": "1-r6", "action": "use_code", "codes": ["wp.01 "], "quote": "it's the toilet floor"}))[0])
    assert r.suggestions[2].action == "use_code" and r.suggestions[2].codes == ["WP.01"]


def test_reread_keeps_a_decision_made_while_the_model_reads():
    SR.record("1", REPLY)

    def parse(**kw):
        F.decide("1", "1-r5", "skip")  # the buyer acts during the call
        return SimpleNamespace(status="completed", output=[], output_parsed=GOOD)

    SR.reread("1", 0, client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    st = F.load_state("1")
    assert st["decisions"]["1-r5"]["action"] == "skip" and st["site_replies"][0]["read"] == "read"


def test_answer_dated_by_the_email_not_the_sync(smtp, monkeypatch):
    sent = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    cfg = mailbox.MailConfig(address="desk@test.example", password="x", watch_from=[])
    raw = _eml("Re: Waterproofing, Tower-1", "site@example.com", REPLY, in_reply_to=sent.message_id)
    raw = raw.replace(b"Subject:", b"Date: Mon, 16 Nov 2026 10:30:00 +0530\nSubject:", 1)
    mailbox.sync(cfg, imap_factory=Imap(raw))
    assert F.load_state("1")["site_replies"][0]["at"] == "2026-11-16T05:00:00+00:00"


def test_blocked_sender_never_reaches_a_case(smtp, monkeypatch):
    sent = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    monkeypatch.setattr(mailbox, "evaluate", lambda rules, facts: {"decision": "block", "reason": "blocked", "rule_id": "r1"})
    cfg = mailbox.MailConfig(address="desk@test.example", password="x", watch_from=[])
    out = mailbox.sync(cfg, imap_factory=Imap(_eml("Re: x", "site@example.com", REPLY, in_reply_to=sent.message_id)))
    assert out["site_replies"] == 0 and out["skipped"] == 1 and not F.load_state("1").get("site_replies")


def test_answer_that_cannot_be_kept_is_retried(smtp, monkeypatch):
    sent = R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", smtp_factory=smtp)
    cfg = mailbox.MailConfig(address="desk@test.example", password="x", watch_from=[])
    imap = Imap(_eml("Re: x", "site@example.com", REPLY, in_reply_to=sent.message_id))
    real = SR.record
    monkeypatch.setattr(SR, "record", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    mailbox.sync(cfg, imap_factory=imap)
    assert json.loads((mailbox.STORE / "77.json").read_text())["status"] == "site_reply_failed"
    monkeypatch.setattr(SR, "record", real)
    assert mailbox.sync(cfg, imap_factory=imap)["site_replies"] == 1
    assert json.loads((mailbox.STORE / "77.json").read_text())["status"] == "site_reply"


# ── demo: simulated answer ─────────────────────────────────────────────────────────────────────


def test_simulated_answer_round_trip_without_a_key():
    with pytest.raises(F.CaseError, match="first"):
        SR.simulate("1")
    R.send("1", "site@example.com", "Re: Waterproofing, Tower-1", "body", mark_only=True)
    r = SR.simulate("1")
    assert r.via == "simulated" and r.read == "read" and "without the model" in r.error
    assert [s.ref for s in r.suggestions] == ["1-r4", "1-r5", "1-r6", "check:basics"]
    assert all(s.action != "unclear" for s in r.suggestions)
    assert all(SR.quote_found(s.quote, SR._norm(r.body)) for s in r.suggestions)  # every answer is in the text
    c = F.build_case("1")
    assert c.site_replied
    for s in r.suggestions:  # what Accept does in the UI
        if s.ref.startswith("check:"):
            c = F.resolve_check("1", s.ref[6:], True)
        else:
            c = F.decide("1", s.ref, s.action, s.codes, note=s.note or None)
    assert c.stage == "approve"


def test_simulated_answer_goes_through_the_model_when_set():
    R.send("1", "site@example.com", "Re: x", "body", mark_only=True)
    client, calls = fake_client(GOOD)
    r = SR.simulate("1", client=client)
    # The model's answer is used (and checked), not the template's own mapping: GOOD quotes another reply.
    assert calls and r.error is None and "aren't in the reply" in r.suggestions[0].why_unclear


def test_simulate_is_for_demo_emails_only():
    with pytest.raises(F.CaseError, match="demo"):
        SR.simulate("m41")


def test_api_simulate():
    app = FastAPI()
    app.include_router(cases_api.router)
    cl = TestClient(app)
    assert cl.get("/api/cases/1").json()["demo"] is True
    assert cl.post("/api/cases/1/site-reply/simulate").status_code == 400  # nothing sent yet
    cl.post("/api/cases/1/reply", json={"to": "site@example.com", "subject": "Re: x", "body": "b", "mark_only": True})
    c = cl.post("/api/cases/1/site-reply/simulate").json()
    assert c["site_replied"] and c["site_replies"][0]["via"] == "simulated"
