from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from procurement import mailbox
from procurement.email_rules import api as rules_api
from procurement.email_rules import rules as R
from procurement.intake import extract

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
RAW = (SAMPLES / "riverside_t2_waterproofing.eml").read_bytes()

NEWSLETTER = b"""From: Cement Weekly <news@cementweekly.com>
To: requirements@example.com
Subject: This week's prices
MIME-Version: 1.0
Content-Type: text/plain

Unsubscribe any time.
"""


def facts(**kw) -> R.EmailFacts:
    base = dict(sender="Site QS <qs@riverside.example.com>", subject="Requirement: cement", body="Please arrange 400 bags",
                to=["requirements@example.com"], attachments=["T2_quantities.xlsx"])
    return R.EmailFacts(**(base | kw))


def rule(action, field, value, match="contains", enabled=True, id=None) -> R.Rule:
    r = R.new_rule({"action": action, "field": field, "match": match, "value": value, "enabled": enabled})
    return r.model_copy(update={"id": id}) if id else r


# ── Precedence ────────────────────────────────────────────────────────────────────────────────


def test_no_rules_processes_everything():
    d = R.evaluate([], facts())
    assert d == {"decision": "process", "rule_id": None, "reason": "Read: no rule stops it."}


def test_block_beats_allow():
    rules = [rule("allow", "sender_domain", "riverside.example.com", "equals", id="a"), rule("block", "subject", "cement", id="b")]
    d = R.evaluate(rules, facts())
    assert d["decision"] == "block" and d["rule_id"] == "b" and 'subject contains "cement"' in d["reason"]


def test_allow_rules_hold_everything_else():
    rules = [rule("allow", "sender", "qs@riverside.example.com", "equals", id="a")]
    assert R.evaluate(rules, facts())["decision"] == "process"
    held = R.evaluate(rules, facts(sender="someone@else.com"))
    assert held == {"decision": "hold", "rule_id": None, "reason": "Held: no allow rule matched."}


def test_disabled_rules_are_ignored():
    rules = [rule("block", "subject", "cement", enabled=False), rule("allow", "sender", "nobody", enabled=False)]
    assert R.evaluate(rules, facts())["decision"] == "process"


@pytest.mark.parametrize(
    "field,match,value,expected",
    [
        ("sender", "contains", "SITE QS", True),  # display name, any case
        ("sender", "equals", "QS@Riverside.Example.com", True),
        ("sender", "equals", "Site QS", False),  # equals looks at the address only
        ("sender_domain", "equals", "riverside.example.com", True),
        ("sender_domain", "starts_with", "river", True),
        ("subject", "starts_with", "requirement:", True),
        ("body", "regex", r"\d{3} bags", True),
        ("to", "starts_with", "requirements@", True),
        ("attachment_type", "equals", ".XLSX", True),
        ("attachment_type", "equals", "pdf", False),
        ("has_attachment", "equals", "yes", True),
        ("has_attachment", "equals", "no", False),
    ],
)
def test_fields_and_matches(field, match, value, expected):
    assert R.matches(rule("block", field, value, match), facts()) is expected


# ── Validation ────────────────────────────────────────────────────────────────────────────────


def test_bad_regex_is_refused_with_a_clear_message():
    with pytest.raises(R.RuleError, match="pattern isn't valid"):
        R.add_rule({"action": "block", "field": "subject", "match": "regex", "value": "([unclosed"})
    assert R.load_rules() == []


def test_other_validation():
    with pytest.raises(R.RuleError, match="yes or no"):
        R.validate({"action": "allow", "field": "has_attachment", "value": "maybe"})
    with pytest.raises(R.RuleError, match="Type a value"):
        R.validate({"action": "allow", "field": "subject", "value": "  "})
    with pytest.raises(R.RuleError):
        R.validate({"action": "skip", "field": "subject", "value": "x"})


def test_store_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "rules.json"
    monkeypatch.setenv("EMAIL_RULES_PATH", str(path))
    r = R.add_rule({"action": "block", "field": "sender_domain", "match": "equals", "value": "spam.com", "note": "vendor spam"})
    assert path.exists() and r.created and r.enabled
    assert R.toggle_rule(r.id).enabled is False
    u = R.update_rule(r.id, {"action": "block", "field": "subject", "value": "newsletter"})
    assert u.id == r.id and u.created == r.created and u.field == "subject"
    R.delete_rule(r.id)
    assert R.load_rules() == []
    with pytest.raises(KeyError):
        R.delete_rule(r.id)


# ── WATCH_FROM compatibility ──────────────────────────────────────────────────────────────────


def test_watch_from_acts_as_read_only_allow_rules(monkeypatch):
    monkeypatch.setenv("WATCH_FROM", "qs@riverside.example.com, lakeview.site@example.com")
    env = R.env_rules()
    assert [(r.action, r.field, r.value, r.source) for r in env] == [
        ("allow", "sender", "qs@riverside.example.com", "env"),
        ("allow", "sender", "lakeview.site@example.com", "env"),
    ]
    rules = R.active_rules()
    assert R.evaluate(rules, facts())["decision"] == "process"
    assert R.evaluate(rules, facts(sender="x@other.com"))["decision"] == "hold"
    with pytest.raises(R.RuleError, match=".env"):
        R.toggle_rule("env-1")


# ── Mailbox integration ───────────────────────────────────────────────────────────────────────


class FakeImap:
    """Two messages: 41 is the riverside sample, 42 a newsletter."""

    def __init__(self, host):
        pass

    def login(self, user, pw):
        pass

    def select(self, folder, readonly=False):
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b"41 42"]
        raw = RAW if args[0] == "41" else NEWSLETTER
        return "OK", [(b"x (BODY[] {1})", raw), b")"]

    def logout(self):
        pass


class CountingModel:
    """A stand-in OpenAI client: counts model calls and fails the test if a skipped email reaches the model."""

    def __init__(self):
        self.calls = 0
        ref = extract.extract_replay(SAMPLES / "riverside_t2_waterproofing.extraction.json")

        def parse(**k):
            self.calls += 1
            assert "cementweekly" not in json.dumps(k, default=str)
            return SimpleNamespace(status="completed", output=[], output_parsed=ref)

        self.responses = SimpleNamespace(parse=parse)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mailbox, "STORE", tmp_path / "inbox")
    return tmp_path / "inbox"


def test_sync_skips_blocked_mail_without_calling_the_model(store, monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: True)
    R.add_rule({"action": "block", "field": "sender_domain", "match": "equals", "value": "cementweekly.com", "note": "newsletter"})
    cfg = mailbox.MailConfig(address="t@gmail.com", password="x", watch_from=[])
    client = CountingModel()
    out = mailbox.sync(cfg, imap_factory=FakeImap, client=client)
    assert out == {"fetched": 2, "read": 1, "failed": 0, "skipped": 1, "site_replies": 0, "waiting_for_key": 0}
    assert client.calls == 1

    items = {i["uid"]: i for i in mailbox.list_inbox()}
    assert items["42"]["status"] == "skipped" and items["42"]["decision"] == "block"
    assert "cementweekly.com" in items["42"]["reason"]
    assert items["41"]["status"] == "read" and items["41"]["decision"] == "process"
    rec = json.loads((store / "42.json").read_text())
    assert rec["decision"]["rule_id"] and rec["facts"]["to"] == ["requirements@example.com"]

    # Skipped mail is not retried.
    assert mailbox.sync(cfg, imap_factory=FakeImap, client=client)["fetched"] == 0
    assert client.calls == 1


def test_sync_holds_mail_no_allow_rule_matches_even_without_key(store, monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    cfg = mailbox.MailConfig(address="t@gmail.com", password="x", watch_from=["riverside.site@example.com"])
    out = mailbox.sync(cfg, imap_factory=FakeImap)
    assert out == {"fetched": 2, "read": 0, "failed": 0, "skipped": 1, "site_replies": 0, "waiting_for_key": 1}
    items = {i["uid"]: i for i in mailbox.list_inbox()}
    assert items["42"]["decision"] == "hold" and items["42"]["reason"] == "Held: no allow rule matched."
    assert items["41"]["status"] == "needs_key" and items["41"]["rule_id"] == "env-1"


# ── API ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def http(store):
    app = FastAPI()
    app.include_router(rules_api.router)
    return TestClient(app)


def test_crud_and_toggle(http):
    r = http.post("/api/email-rules", json={"action": "block", "field": "subject", "match": "contains", "value": "newsletter"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["text"] == 'subject contains "newsletter"'
    assert http.post(f"/api/email-rules/{rid}/toggle").json()["enabled"] is False
    assert http.post(f"/api/email-rules/{rid}/toggle", json={"enabled": True}).json()["enabled"] is True
    up = http.put(f"/api/email-rules/{rid}", json={"action": "allow", "field": "sender_domain", "match": "equals", "value": "example.com"})
    assert up.json()["action"] == "allow"
    assert len(http.get("/api/email-rules").json()["rules"]) == 1
    assert http.delete(f"/api/email-rules/{rid}").status_code == 200
    assert http.delete(f"/api/email-rules/{rid}").status_code == 404


def test_bad_regex_returns_422(http):
    r = http.post("/api/email-rules", json={"action": "block", "field": "body", "match": "regex", "value": "a{2,1}"})
    assert r.status_code == 422 and "pattern isn't valid" in r.json()["detail"]


def test_env_rules_listed_read_only(http, monkeypatch):
    monkeypatch.setenv("WATCH_FROM", "riverside.store@example.com")
    data = http.get("/api/email-rules").json()
    assert data["rules"] == [] and data["env_rules"][0]["read_only"] is True
    assert "Block rules always win" in data["how_rules_combine"]
    assert http.post("/api/email-rules/env-1/toggle").status_code == 400


def test_preview_draft_without_saving(http, store):
    demo = json.loads((SAMPLES / "demo_inbox.json").read_text())
    store.mkdir(parents=True)
    (store / "42.json").write_text(json.dumps({"uid": "42", "status": "skipped", "subject": "This week's prices",
                                                "facts": {"sender": "news@cementweekly.com", "subject": "This week's prices"}}))
    base = http.post("/api/email-rules/preview").json()
    assert len(base["emails"]) == len(demo) + 1 and base["counts"]["process"] == len(demo) + 1 and base["proposed"] is False

    draft = {"action": "allow", "field": "sender_domain", "match": "equals", "value": "example.com"}
    out = http.post("/api/email-rules/preview", json={"draft": draft}).json()
    by = {(e["source"], e["uid"]): e for e in out["emails"]}
    assert by[("inbox", "42")]["decision"] == "hold" and by[("inbox", "42")]["changed"] is True
    assert all(e["decision"] == "process" for e in out["emails"] if e["source"] == "demo")
    assert R.load_rules() == []  # nothing saved

    whole = http.post("/api/email-rules/preview", json={"rules": [{"action": "block", "field": "subject", "value": "tower"}]}).json()
    assert whole["counts"]["block"] == sum("tower" in d["result"]["email"]["subject"].lower() for d in demo)

    bad = http.post("/api/email-rules/preview", json={"draft": {"action": "block", "field": "subject", "match": "regex", "value": "("}})
    assert bad.status_code == 422


def test_preview_draft_replaces_rule_being_edited(http):
    rid = http.post("/api/email-rules", json={"action": "block", "field": "subject", "value": "cement"}).json()["id"]
    out = http.post("/api/email-rules/preview", json={"draft": {"action": "block", "field": "subject", "value": "zzz"}, "draft_id": rid}).json()
    assert out["counts"]["block"] == 0


def test_summary_shape(http):
    http.post("/api/email-rules", json={"action": "block", "field": "subject", "value": "cement"})
    s = http.get("/api/email-rules/summary").json()
    assert set(s) == {"title", "headline", "stats", "ready"} and s["ready"] is True
    stats = {x["label"]: x for x in s["stats"]}
    assert stats["Rules on"]["value"] == 1 and stats["Blocked"]["value"] == 1 and stats["Blocked"]["tone"] == "bad"
    assert all(set(x) == {"label", "value", "tone"} for x in s["stats"])
