import pytest


@pytest.fixture(autouse=True)
def _isolated_email_rules(tmp_path, monkeypatch):
    """Never read or write the real data/email_rules.json or the real pack quotes; ignore WATCH_FROM / HOD_EMAIL from .env."""
    monkeypatch.setenv("EMAIL_RULES_PATH", str(tmp_path / "email_rules.json"))
    monkeypatch.delenv("WATCH_FROM", raising=False)
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path / "no-pack"))  # never link the real tender folder's quotes
    monkeypatch.delenv("HOD_EMAIL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # .env has a real key: tests never go live
