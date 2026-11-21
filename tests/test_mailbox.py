from __future__ import annotations

import imaplib
from pathlib import Path
from types import SimpleNamespace

import pytest

from procurement import mailbox
from procurement.intake import extract

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
RAW = (SAMPLES / "riverside_t2_waterproofing.eml").read_bytes()
CFG = mailbox.MailConfig(address="test@gmail.com", password="x", watch_from=["riverside.site@example.com"])


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL: one folder, two messages from the watched sender."""

    instances: list["FakeImap"] = []

    def __init__(self, host, fail_login=False):
        self.host, self.fail_login, self.commands = host, fail_login, []
        FakeImap.instances.append(self)

    def login(self, user, pw):
        if self.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        self.commands.append((cmd, args))
        if cmd == "SEARCH":
            return "OK", [b"41 42"]
        return "OK", [(b"41 (BODY[] {123}", RAW), b")"]

    def logout(self):
        pass


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(mailbox, "STORE", tmp_path / "inbox")
    FakeImap.instances.clear()


def test_fetch_is_read_only_and_filters_by_sender():
    msgs = mailbox.fetch_messages(CFG, skip={"41"}, imap_factory=FakeImap)
    imap = FakeImap.instances[0]
    assert imap.readonly is True
    search = next(a for c, a in imap.commands if c == "SEARCH")
    assert 'FROM "riverside.site@example.com"' in search[1]
    fetches = [a for c, a in imap.commands if c == "FETCH"]
    assert fetches == [("42", "(BODY.PEEK[])")]  # 41 skipped; PEEK keeps it unread
    assert msgs == [("42", RAW)]


def test_bad_login_explains_app_password():
    with pytest.raises(mailbox.MailboxError, match="app password"):
        mailbox.fetch_messages(CFG, set(), imap_factory=lambda h: FakeImap(h, fail_login=True))


def test_sync_without_key_saves_and_retries_later(monkeypatch):
    monkeypatch.setattr(extract, "has_credentials", lambda: False)
    out = mailbox.sync(CFG, imap_factory=FakeImap)
    assert out == {"fetched": 2, "read": 0, "failed": 0, "skipped": 0, "waiting_for_key": 2}
    assert {i["status"] for i in mailbox.list_inbox()} == {"needs_key"}

    # A key appears: the same two emails are read on the next sync, and not again after that.
    ref = extract.extract_replay(SAMPLES / "riverside_t2_waterproofing.extraction.json")
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=lambda **k: SimpleNamespace(stop_reason="end_turn", parsed_output=ref))))
    monkeypatch.setattr(extract, "has_credentials", lambda: True)
    assert mailbox.sync(CFG, imap_factory=FakeImap, client=client)["read"] == 2
    assert mailbox.sync(CFG, imap_factory=FakeImap, client=client)["fetched"] == 0
    item = mailbox.list_inbox()[0]
    assert item["status"] == "read" and item["blocking"] == 1 and item["project"] == "Vinayak Riverside"


def test_config_rejects_unsafe_sender(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh")
    monkeypatch.setenv("WATCH_FROM", 'ok@x.com, bad") OR ALL (')
    with pytest.raises(mailbox.MailboxError):
        mailbox.MailConfig.from_env()
    monkeypatch.setenv("WATCH_FROM", "ok@x.com, b@y.com")
    cfg = mailbox.MailConfig.from_env()
    assert cfg.watch_from == ["ok@x.com", "b@y.com"] and cfg.password == "abcdefgh"


def test_dedicated_address_reads_everything(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "requirements@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.delenv("WATCH_FROM", raising=False)
    cfg = mailbox.MailConfig.from_env()
    assert cfg is not None and cfg.watch_from == []
    mailbox.fetch_messages(cfg, set(), imap_factory=FakeImap)
    searches = [a[1] for c, a in FakeImap.instances[0].commands if c == "SEARCH"]
    assert len(searches) == 1 and searches[0].startswith("(SINCE ") and "FROM" not in searches[0]
