"""Gmail connector: pull emails from the watched senders and run each through intake once.

Connects over IMAP with a Gmail app password (Google Account → Security → 2-Step Verification →
App passwords). Messages are fetched with BODY.PEEK, so nothing is marked as read. Each result is
saved to data/inbox/<uid>.json; a UID already saved is never processed again.

Before any Claude call, each email is checked against the email rules (procurement/email_rules).
An email a rule blocks or holds is saved with status "skipped" and the decision, costs nothing,
and is not retried. Delete its data/inbox/<uid>.json to have the next sync look at it again.

Config (.env at the repo root, never committed):
    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
    WATCH_FROM=site.qs@example.com,other@example.com   # optional; empty = every email in the folder
    MAIL_SINCE_DAYS=30              # optional, default 30
    IMAP_HOST=imap.gmail.com        # optional
    IMAP_FOLDER=INBOX               # optional; "[Gmail]/All Mail" to include archived mail

CLI:  uv run python -m procurement.mailbox
"""

from __future__ import annotations

import imaplib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .email_rules import EmailFacts, RuleError, active_rules, evaluate
from .intake import extract
from .intake.pipeline import load_email, run_intake

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "inbox"
load_dotenv(ROOT / ".env")


class MailboxError(RuntimeError):
    pass


@dataclass
class MailConfig:
    address: str
    password: str
    watch_from: list[str]
    since_days: int = 30
    host: str = "imap.gmail.com"
    folder: str = "INBOX"

    @classmethod
    def from_env(cls) -> "MailConfig | None":
        addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
        watch = [w.strip() for w in os.environ.get("WATCH_FROM", "").split(",") if w.strip()]
        if not (addr and pw):
            return None
        bad = [w for w in watch if not _safe_sender(w)]
        if bad:
            raise MailboxError(f"WATCH_FROM entries can't contain spaces, quotes or brackets: {bad}")
        return cls(
            address=addr,
            password=pw.replace(" ", ""),
            watch_from=watch,
            since_days=int(os.environ.get("MAIL_SINCE_DAYS", "30")),
            host=os.environ.get("IMAP_HOST", "imap.gmail.com"),
            folder=os.environ.get("IMAP_FOLDER", "INBOX"),
        )

    def public(self) -> dict:
        return {"address": self.address, "watch_from": self.watch_from, "since_days": self.since_days, "folder": self.folder}


def _since(days: int) -> str:
    return (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")


def fetch_messages(cfg: MailConfig, skip: set[str], imap_factory=imaplib.IMAP4_SSL) -> list[tuple[str, bytes]]:
    """Return (uid, raw RFC 822 bytes) for watched senders' mail not in `skip`, oldest first."""
    try:
        imap = imap_factory(cfg.host)
        imap.login(cfg.address, cfg.password)
    except imaplib.IMAP4.error as e:
        raise MailboxError("Gmail refused the login. Use an app password, not your normal password, and check GMAIL_ADDRESS.") from e
    except OSError as e:
        raise MailboxError(f"Couldn't reach {cfg.host}. Check the network.") from e
    try:
        typ, _ = imap.select(f'"{cfg.folder}"', readonly=True)
        if typ != "OK":
            raise MailboxError(f"Folder {cfg.folder!r} not found in the mailbox.")
        # A dedicated intake address needs no sender filter: every email in the folder is read.
        queries = [f'(FROM "{s}" SINCE {_since(cfg.since_days)})' for s in cfg.watch_from] or [f"(SINCE {_since(cfg.since_days)})"]
        uids: list[str] = []
        for q in queries:
            typ, data = imap.uid("SEARCH", None, q)
            if typ == "OK" and data and data[0]:
                uids += data[0].decode().split()
        out = []
        for uid in sorted(set(uids), key=int):
            if uid in skip:
                continue
            typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((part[1] for part in data or [] if isinstance(part, tuple)), None)
            if typ == "OK" and raw:
                out.append((uid, raw))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ── local store ────────────────────────────────────────────────────────────────────────────────


def _path(uid: str) -> Path:
    return STORE / f"{int(uid)}.json"


def saved_uids() -> set[str]:
    return {p.stem for p in STORE.glob("*.json")} if STORE.exists() else set()


def load_result(uid: str) -> dict | None:
    p = _path(uid)
    return json.loads(p.read_text()) if p.exists() else None


def list_inbox() -> list[dict]:
    items = []
    for p in STORE.glob("*.json") if STORE.exists() else []:
        r = json.loads(p.read_text())
        res = r.get("result") or {}
        checks = res.get("checks", [])
        items.append(
            {
                "uid": r["uid"],
                "subject": r.get("subject"),
                "sender": r.get("sender"),
                "date": r.get("date"),
                "status": r["status"],
                "error": r.get("error"),
                # What the email rules decided ("process" | "block" | "hold"), None for older records.
                "decision": (r.get("decision") or {}).get("decision"),
                "reason": (r.get("decision") or {}).get("reason"),
                "rule_id": (r.get("decision") or {}).get("rule_id"),
                "blocking": sum(c["severity"] == "block" for c in checks),
                "to_confirm": sum(c["severity"] == "warn" for c in checks),
                "project": ((res.get("requirement") or {}).get("project") or {}).get("value"),
            }
        )
    return sorted(items, key=lambda x: int(x["uid"]), reverse=True)


def sync(cfg: MailConfig, imap_factory=imaplib.IMAP4_SSL, client=None) -> dict:
    """Fetch new mail (from watched senders, or everything if none are set), apply the email rules,
    and run intake on each email the rules let through."""
    try:
        rules = active_rules(cfg.watch_from)
    except RuleError as e:
        raise MailboxError(f"Email rules couldn't be loaded: {e}") from e
    STORE.mkdir(parents=True, exist_ok=True)
    live = extract.has_credentials() or client is not None
    # Emails saved while there was no API key are retried once a key is set. Skipped ones never are.
    retry = {i["uid"] for i in list_inbox() if i["status"] == "needs_key"} if live else set()
    new = fetch_messages(cfg, saved_uids() - retry, imap_factory)
    done = failed = skipped = 0
    for uid, raw in new:
        mail = load_email(raw)
        facts = EmailFacts.from_email(mail, raw)
        decision = evaluate(rules, facts)
        rec = {"uid": uid, "subject": mail.subject, "sender": mail.sender, "date": mail.date,
               "decision": decision, "facts": facts.as_dict() | {"body": facts.body[:20000]}}
        if decision["decision"] != "process":
            rec |= {"status": "skipped", "error": decision["reason"]}
            skipped += 1
        elif not live:
            rec |= {"status": "needs_key", "error": "Fetched, not read yet: set ANTHROPIC_API_KEY and sync again."}
        else:
            try:
                rec |= {"status": "read", "result": run_intake(mail, client=client).model_dump(mode="json")}
                done += 1
            except extract.IntakeError as e:
                rec |= {"status": "failed", "error": str(e)}
                failed += 1
        _path(uid).write_text(json.dumps(rec, indent=2))
    return {"fetched": len(new), "read": done, "failed": failed, "skipped": skipped,
            "waiting_for_key": len(new) - done - failed - skipped}


def _safe_sender(s: str) -> bool:
    return bool(re.fullmatch(r"[^\"()\s]+", s))


if __name__ == "__main__":
    cfg = MailConfig.from_env()
    if cfg is None:
        raise SystemExit("Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD and WATCH_FROM in .env first (see .env.example).")
    print(json.dumps(sync(cfg), indent=2))
