"""The reply to the site for lines that can't go to vendors yet: a code conflict, a partial match or
no item code, plus problems with the email itself (the intake checks).

The draft is plain template text (no LLM), so every question traces back to a check. A person edits
it before it goes. Sending is limited to test inboxes: the recipient must be listed in
REPLY_ALLOWED_TO (addresses or @domains). Otherwise the reply can only be copied or marked as sent.
"""

from __future__ import annotations

import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path

from pydantic import BaseModel

from . import flow as F

INTRO = {
    "conflict": "The item code is unclear",
    "partial": "Only part of this matches an item code",
    "no_match": "We have no item code for this",
}
ASK = {
    "conflict": "Please confirm which item this is, so we don't order against the wrong code.",
    "partial": "Please confirm exactly what is covered, so we use the right code (or open a new one).",
    "no_match": "Please confirm the specification (material, method and unit) so we can open a new code.",
}


class Draft(BaseModel):
    to: str | None
    subject: str
    body: str
    lines: list[str]  # ids of the lines asked about
    needed: bool


def _qty(ln: F.CaseLine) -> str:
    if ln.quantity is None:
        return ""
    q = f"{ln.quantity:,.2f}".rstrip("0").rstrip(".")
    return f" ({q} {ln.uom})" if ln.uom else f" ({q})"


def _options(ln: F.CaseLine) -> list[str]:
    m = ln.match
    out = []
    if m.code:
        out.append(f"{m.code} \"{m.erp_text}\"" + (f" together with {', '.join(m.also)}" if m.also else ""))
    for c in m.candidates:
        if c.code != m.code and len(out) < 3:
            out.append(f"{c.code} \"{c.text}\"")
    if m.suggestion:
        out.append(f"a new code {m.suggestion.code}")
    return out


def _split(ln: F.CaseLine) -> str:
    by = {c.code: c.text for c in ln.match.candidates}
    return ", ".join(f"{c}: {by.get(c, '').lower() or c}" for c in [ln.match.code, *ln.match.also])


def draft(case: F.Case) -> Draft:
    asked = [ln for ln in case.lines if ln.status == "to_reply"]
    checks = [c for c in case.email_checks if not c.get("resolved")]
    subject = case.subject or "your requirement"
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    name = re.sub(r"<.*>", "", case.sender or "").strip().strip('"')
    greet = f"Hi {name}," if name else "Hi,"
    # Only a line that needs a decision, or an email problem that stops the tender, needs a reply.
    # Warnings (e.g. no finish date) ride along in a reply that's needed anyway, but never start one.
    if not asked and not any(c.get("severity") == "block" for c in checks):
        body = f"{greet}\n\nThanks, we have what we need for this requirement and will take it to vendors for quotes.\n\nRegards,\nProcurement"
        return Draft(to=case.sender, subject=subject, body=body, lines=[], needed=False)

    parts = [greet, "", "Thanks for the requirement. Before we send it to vendors, please confirm these points:", ""]
    n = 0
    for ln in asked:
        n += 1
        kinds = [i.kind for i in ln.issues]
        kind = "conflict" if "conflict" in kinds else (kinds[0] if kinds else None)
        parts.append(f"{n}. {ln.text}{_qty(ln)}")
        if kind == "partial" and ln.match.also and "conflict" not in kinds:
            codes = [ln.match.code, *ln.match.also]
            parts.append(f"   Our item list splits this into {' and '.join(codes)} ({_split(ln)}). Is it floor, wall or both? Please give the quantity for each.")
            parts.append("")
            continue
        if kind:
            parts.append(f"   {INTRO[kind]}. " + " ".join(i.text for i in ln.issues))
            opts = _options(ln)
            if opts:
                parts.append("   Options we see: " + "; ".join(opts) + ".")
            parts.append(f"   {ASK[kind]}")
        if ln.decision and ln.decision.note:
            parts.append(f"   {ln.decision.note}")
        parts.append("")
    for c in checks:
        if c.get("resolved"):
            continue
        n += 1
        parts.append(f"{n}. {c['title']}: {c['detail']}")
        parts.append("")
    ready = [ln for ln in case.lines if ln.status in ("ready", "decided")]
    if ready:
        parts.append(f"The other {len(ready)} line{'s' * (len(ready) != 1)} are clear; we'll take everything to vendors once the points above are confirmed." if asked
                     else "All lines are clear; we'll go to vendors once the points above are confirmed.")
        parts.append("")
    parts += ["Regards,", "Procurement"]
    return Draft(to=case.sender, subject=subject, body="\n".join(parts), lines=[ln.id for ln in asked], needed=True)


# ── Sending (test inboxes only) ────────────────────────────────────────────────────────────────


class SendError(RuntimeError):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def allowed_to() -> list[str]:
    return [a.strip().lower() for a in os.environ.get("REPLY_ALLOWED_TO", "").split(",") if a.strip()]


def is_allowed(addr: str) -> bool:
    a = addr.lower()
    return any(a == x or (x.startswith("@") and a.endswith(x)) for x in allowed_to())


def can_send() -> dict:
    smtp = bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))
    return {"smtp": smtp, "allowed_to": allowed_to()}


def deliver(to: str, subject: str, body: str, *, mark_only: bool = False, smtp_factory=smtplib.SMTP_SSL,
            attachments: list[Path] = (), headers: dict[str, str] | None = None) -> tuple[str, str]:
    """Send one email (with any attached files) to a test inbox, or only check it, with mark_only.
    `headers` adds threading headers (Message-ID, In-Reply-To, References). Returns (address, via)."""
    addr = parseaddr(to)[1]
    if not addr or "@" not in addr:
        raise SendError("Give one email address to send to.")
    if not body.strip():
        raise SendError("The email is empty.")
    if mark_only:
        return addr, "marked"
    if not is_allowed(addr):
        raise SendError(f"Sending is limited to test inboxes. {addr} isn't in REPLY_ALLOWED_TO. Copy the email instead, or mark it as sent.", 403)
    user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        raise SendError("Email sending isn't set up: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env.", 409)
    if any(ch in subject + addr for ch in "\r\n"):
        raise SendError("The subject can't contain line breaks.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, addr, subject
    for k, v in (headers or {}).items():
        if v and not any(ch in v for ch in "\r\n"):
            msg[k] = v
    msg.set_content(body)
    for p in attachments:
        kind = (mimetypes.guess_type(p.name)[0] or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=kind[0], subtype=kind[1], filename=p.name)
    try:
        with smtp_factory(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), timeout=30) as s:
            s.login(user, pw.replace(" ", ""))
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise SendError(f"The email server refused it: {e}", 502) from e
    return addr, "smtp"


def send(uid: str, to: str, subject: str, body: str, *, mark_only: bool = False, smtp_factory=smtplib.SMTP_SSL,
         case: F.Case | None = None) -> F.SentReply:
    """The reply to the site, sent (test inboxes only) or marked as sent, and recorded on the case
    with what it asked (so a question added later shows the case needs another reply)."""
    case = case or F.build_case(uid)
    # A Message-ID of our own, kept on the case: the site's answer quotes it in In-Reply-To, which is how
    # procurement/cases/site_reply.match_case links the answer back here instead of opening a new requirement.
    domain = (os.environ.get("GMAIL_ADDRESS") or "").rpartition("@")[2] or None
    mid = make_msgid(domain=domain)
    orig = (F.record(uid).get("message_id") or "").strip()
    thread = {"Message-ID": mid, "In-Reply-To": orig, "References": orig}
    addr, via = deliver(to, subject, body, mark_only=mark_only, smtp_factory=smtp_factory, headers=thread)
    sent = F.SentReply(to=addr, subject=subject, body=body, at=F.now(), via=via, asked=F.asked_ids(case),
                       message_id=mid if via == "smtp" else None)
    state = F.load_state(uid)
    state.setdefault("replies", []).append(sent.model_dump())
    state.pop("redecided", None)
    F.save_state(uid, state)
    return sent
