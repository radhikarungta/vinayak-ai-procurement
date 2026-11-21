"""Turn a site email into plain parts: headers, body, the reply thread and attachments.

Accepts either a real RFC 822 message (.eml, what Gmail/Outlook "download message" gives you)
or pasted text. Pasted text may start with `SUBJECT:` / `FROM:` / `DATE:` lines, the way the
hackathon data pack's emails are written out.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from html import unescape

from .models import ThreadMessage

PSEUDO_HEADER = re.compile(r"^(SUBJECT|FROM|TO|CC|DATE|ATTACHMENTS?(?: IN THIS FOLDER)?)\s*:\s*(.*)$", re.I)

# Lines that open a quoted earlier message inside a reply.
THREAD_MARKERS = [
    re.compile(r"^\s*On .{4,120}\bwrote:\s*$", re.I),            # Gmail
    re.compile(r"^\s*-{2,}\s*(Original|Forwarded) Message\s*-{2,}", re.I),
    re.compile(r"^\s*From:\s*\S.*$"),                               # Outlook block
]


@dataclass
class RawAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Email:
    subject: str | None
    sender: str | None
    date: str | None
    body: str
    attachments: list[RawAttachment] = field(default_factory=list)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))


def parse_eml(data: bytes) -> Email:
    msg: EmailMessage = email.message_from_bytes(data, policy=policy.default)  # type: ignore[assignment]
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    attachments = [
        RawAttachment(
            filename=part.get_filename() or "attachment",
            content_type=part.get_content_type(),
            data=part.get_payload(decode=True) or b"",
        )
        for part in msg.iter_attachments()
    ]
    return Email(
        subject=msg.get("Subject"),
        sender=msg.get("From"),
        date=msg.get("Date"),
        body=body.strip(),
        attachments=attachments,
    )


def parse_text(text: str) -> Email:
    """Pasted email text. Leading `KEY: value` lines are read as headers until the first line that isn't one."""
    headers: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = PSEUDO_HEADER.match(lines[i].strip())
        if not m:
            break
        headers[m.group(1).upper().split()[0]] = m.group(2).strip()
        i += 1
    body = "\n".join(lines[i:]).strip()
    body = re.sub(r"^-{8,}\s*\n", "", body)  # the data pack's separator line
    return Email(subject=headers.get("SUBJECT"), sender=headers.get("FROM"), date=headers.get("DATE"), body=body.strip())


def looks_like_eml(data: bytes) -> bool:
    head = data[:2048].decode("utf-8", "ignore")
    return bool(re.search(r"^(Received|Message-ID|MIME-Version|Content-Type):", head, re.M | re.I))


def split_thread(body: str) -> list[ThreadMessage]:
    """Split a reply into its messages, newest first as they appear. Quote markers ('>') are stripped."""
    out: list[ThreadMessage] = []
    header: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        clean = re.sub(r"^\s*(>\s?)+", "", line)
        if any(p.match(clean) for p in THREAD_MARKERS) and buf and any(s.strip() for s in buf):
            out.append(ThreadMessage(header=header, body=_tidy("\n".join(buf))))
            header, buf = clean.strip(), []
            continue
        if not buf and any(p.match(clean) for p in THREAD_MARKERS):
            header = clean.strip()
            continue
        buf.append(clean)
    if any(s.strip() for s in buf):
        out.append(ThreadMessage(header=header, body=_tidy("\n".join(buf))))
    return out


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()
