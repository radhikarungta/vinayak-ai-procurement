"""Email in, structured requirement + BOQ checks out."""

from __future__ import annotations

from pathlib import Path

from . import extract
from .checks import run_checks
from .email_parser import Email, RawAttachment, looks_like_eml, parse_eml, parse_text, split_thread
from .models import Attachment, IntakeResult, ParsedEmail
from .quantity_sheet import parse_quantity_sheet

def load_email(raw: bytes | str, extra_attachments: list[RawAttachment] | None = None) -> Email:
    if isinstance(raw, bytes) and looks_like_eml(raw):
        mail = parse_eml(raw)
    else:
        mail = parse_text(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    mail.attachments.extend(extra_attachments or [])
    return mail


def run_intake(mail: Email, *, replay: Path | None = None, client=None) -> IntakeResult:
    sheet = None
    used = set()
    for a in mail.attachments:
        if a.filename.lower().endswith((".xlsx", ".xlsm")):
            try:
                sheet = parse_quantity_sheet(a.data, a.filename)
            except Exception:  # a corrupt workbook is reported by the attachment-missing check
                sheet = None
            if sheet:
                used.add(a.filename)
                break

    if replay is not None:
        raw_req, mode, model = extract.extract_replay(replay), "replay", None
    else:
        raw_req, mode, model = extract.extract_live(mail, client=client), "live", extract.MODEL

    req, dropped = extract.verify_quotes(raw_req, mail)

    return IntakeResult(
        email=ParsedEmail(
            subject=mail.subject,
            sender=mail.sender,
            date=mail.date,
            body=mail.body,
            thread=split_thread(mail.body),
            attachments=[Attachment(filename=a.filename, content_type=a.content_type, size=len(a.data), used=a.filename in used) for a in mail.attachments],
        ),
        requirement=req,
        dropped_fields=dropped,
        quantities=sheet,
        checks=run_checks(mail, req, sheet),
        extraction_mode=mode,
        model=model,
    )
