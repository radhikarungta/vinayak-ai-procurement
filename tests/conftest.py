import pytest


@pytest.fixture(autouse=True)
def _isolated_email_rules(tmp_path, monkeypatch):
    """Never read or write the real data/email_rules.json or the real pack quotes; ignore WATCH_FROM / HOD_EMAIL from .env."""
    monkeypatch.setenv("EMAIL_RULES_PATH", str(tmp_path / "email_rules.json"))
    monkeypatch.delenv("WATCH_FROM", raising=False)
    monkeypatch.setenv("QUOTES_PACK", str(tmp_path / "no-pack"))  # never link the real tender folder's quotes
    monkeypatch.delenv("HOD_EMAIL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # .env has a real key: tests never go live
    monkeypatch.setenv("AGENT_STEP_DELAY", "0")
    monkeypatch.setenv("AGENT_ON_ARRIVAL", "0")  # no background agent threads outliving a mailbox test  # the agent's pause between steps is for people watching
    monkeypatch.setenv("CASES_DIR", str(tmp_path / "cases"))  # a synced reply is matched against saved cases: never the real ones
