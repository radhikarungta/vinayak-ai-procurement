import pytest


@pytest.fixture(autouse=True)
def _isolated_email_rules(tmp_path, monkeypatch):
    """Never read or write the real data/email_rules.json, and ignore WATCH_FROM from .env."""
    monkeypatch.setenv("EMAIL_RULES_PATH", str(tmp_path / "email_rules.json"))
    monkeypatch.delenv("WATCH_FROM", raising=False)
