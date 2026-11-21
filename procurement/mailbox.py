"""Gmail connector: pull emails from the watched senders and run each through intake once.

Connects over IMAP with a Gmail app password (Google Account → Security → 2-Step Verification →
App passwords). Messages are fetched with BODY.PEEK, so nothing is marked as read. Each result is
saved to data/inbox/<uid>.json; a UID already saved is never processed again.

An email that answers a reply we sent to the site (In-Reply-To / References carry our Message-ID, or
the same subject from the address we wrote to) is not a new requirement: it is kept on that case as
the site's answer (procurement/cases/site_reply) and saved here with status "site_reply".

A new requirement that was read is handed to the requirement agent (procurement/cases/agent.py),
which takes it as far as it can without sending an email.

Before any model call, each email is checked against the email rules (procurement/email_rules).
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

import email as emaillib
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

from .email_rules import EmailFacts, RuleError, active_rules, evaluate
from .intake import extract
from .intake.pipeline import load_email, run_intake

log = logging.getLogger(__name__)

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


def attachment_path(uid: str, filename: str) -> Path:
    """Where an attachment read by intake (the quantity sheet) is kept, so an RFQ can attach it."""
    name = Path(filename).name
    return STORE / f"{int(uid)}.files" / (name if name.strip(".") else "attachment")


def saved_attachment(uid: str, filename: str | None) -> Path | None:
    p = attachment_path(uid, filename) if filename else None
    return p if p and p.is_file() else None


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
                "case": r.get("case"),  # for a site's answer: the requirement it was attached to
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
    inbox = list_inbox()
    retry = {i["uid"] for i in inbox if i["status"] == "needs_key"} if live else set()
    retry |= {i["uid"] for i in inbox if i["status"] == "site_reply_failed"}  # an answer that couldn't be kept: try again
    new = fetch_messages(cfg, saved_uids() - retry, imap_factory)
    done = failed = skipped = answers = 0
    for uid, raw in new:
        mail = load_email(raw)
        head = _headers(raw)
        facts = EmailFacts.from_email(mail, raw)
        decision = evaluate(rules, facts)
        rec = {"uid": uid, "subject": mail.subject, "sender": mail.sender, "date": mail.date, "message_id": head["message-id"],
               "decision": decision, "facts": facts.as_dict() | {"body": facts.body[:20000]}}
        # A blocked sender never reaches a case. Otherwise an answer to our reply is kept on its case.
        if decision["decision"] != "block" and _site_reply(uid, mail, head, rec, client if live else None):
            answers += 1
            _path(uid).write_text(json.dumps(rec, indent=2))
            continue
        if decision["decision"] != "process":
            rec |= {"status": "skipped", "error": decision["reason"]}
            skipped += 1
        elif not live:
            rec |= {"status": "needs_key", "error": "Fetched, not read yet: set OPENAI_API_KEY and sync again."}
        else:
            try:
                rec |= {"status": "read", "result": run_intake(mail, client=client).model_dump(mode="json")}
                done += 1
            except extract.IntakeError as e:
                rec |= {"status": "failed", "error": str(e)}
                failed += 1
            else:
                _keep_sheet(uid, mail, rec["result"])
        _path(uid).write_text(json.dumps(rec, indent=2))
        if rec.get("status") == "read":
            _run_agent(uid)
    return {"fetched": len(new), "read": done, "failed": failed, "skipped": skipped, "site_replies": answers,
            "waiting_for_key": len(new) - done - failed - skipped - answers}


def _headers(raw: bytes) -> dict[str, str | None]:
    try:
        msg = emaillib.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
    except Exception:
        return {"message-id": None, "in-reply-to": None, "references": None}
    get = lambda k: (str(msg.get(k)).strip() or None) if msg.get(k) else None  # noqa: E731
    return {"message-id": get("Message-ID"), "in-reply-to": get("In-Reply-To"), "references": get("References")}


def _run_agent(uid: str) -> None:
    """A new requirement landed: the agent takes it as far as it can (live mode stops before any email goes out)."""
    from .cases import agent  # noqa: PLC0415 - cases imports this module

    if os.environ.get("AGENT_ON_ARRIVAL", "1") == "0":
        return
    try:
        agent.start(f"m{uid}")
    except Exception as e:  # noqa: BLE001 - the email is saved either way; the agent can be run again from the case
        log.warning("Agent on new requirement m%s failed: %s", uid, e)


def _iso(date_header: str | None) -> str | None:
    """The email's own Date as UTC ISO (what flow compares with our reply's time), None if unreadable."""
    try:
        d = parsedate_to_datetime(date_header) if date_header else None
        return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d and d.tzinfo else None
    except (TypeError, ValueError):
        return None


def _site_reply(uid: str, mail, head: dict, rec: dict, client) -> bool:
    """If this email answers our reply to the site, keep it on that case (and read it) and say so."""
    from .cases import site_reply as SR  # noqa: PLC0415 - cases.flow imports this module

    case_uid = SR.match_case(in_reply_to=head["in-reply-to"], references=head["references"], subject=mail.subject,
                             sender=mail.sender, message_id=head["message-id"])
    if not case_uid:
        return False
    try:
        r = SR.record(case_uid, mail.body, sender=mail.sender, subject=mail.subject, via="email",
                      message_id=head["message-id"], at=_iso(mail.date), client=client)
    except Exception as e:  # noqa: BLE001 - one bad answer must not stop the sync; it is retried on the next one
        log.warning("Site reply %s for case %s couldn't be kept: %s", uid, case_uid, e)
        rec |= {"status": "site_reply_failed", "case": case_uid, "error": f"Answer to requirement {case_uid}, not kept yet (tried again on the next sync): {e}"}
        return True
    rec |= {"status": "site_reply", "case": case_uid, "error": None if r.read != "failed" else r.error}
    return True


def _keep_sheet(uid: str, mail, result: dict) -> None:
    """Keep the quantity sheet intake read, to attach to RFQs. A disk error only loses the attachment,
    not the (paid-for) intake record."""
    for a in mail.attachments:
        if a.filename == ((result.get("quantities") or {}).get("filename")):
            try:
                p = attachment_path(uid, a.filename)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(a.data)
            except OSError as e:
                log.warning("Couldn't keep the quantity sheet of email %s: %s", uid, e)
            return


def _safe_sender(s: str) -> bool:
    return bool(re.fullmatch(r"[^\"()\s]+", s))


if __name__ == "__main__":
    cfg = MailConfig.from_env()
    if cfg is None:
        raise SystemExit("Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD and WATCH_FROM in .env first (see .env.example).")
    print(json.dumps(sync(cfg), indent=2))
