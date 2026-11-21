"""Claude reads the email and fills ExtractedRequirement. Then every quote is checked against the email.

Claude only reads words here. Quantities come from the attached sheet (quantity_sheet.py) and
the checks are plain code (checks.py), so no number in the result comes from the model.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anthropic

from .email_parser import Email
from .models import Cited, CitedList, DroppedField, ExtractedRequirement

MODEL = os.environ.get("INTAKE_MODEL", "claude-opus-5-5")

SYSTEM = """You read requirement emails for the procurement back office of a residential real-estate developer in Kolkata, India.

Site teams email the back office when they need a material or a work package (a "work order", WO). The back office then checks the bill of quantities (BOQ), floats a tender to at least three vendors, builds a comparative statement and gets management approval. Your job is the first step: turn one email thread into a structured requirement the back office can act on.

How to read the email:
- Copy every `quote` verbatim from the email text, exactly as written, including the original spelling and capitalisation. Keep quotes short: the smallest span that supports the value. A program checks each quote against the email, and any field whose quote is not found is thrown away.
- Write `value` in plain words. You may normalise (e.g. "3rd floor through typical floors" -> "3rd floor to the top typical floor"), but don't add facts the email doesn't state.
- If the email doesn't say something, return null (or an empty list). Don't guess from what is usual for the trade.
- Threads: the email may contain replies and forwarded messages. Read all of them. Record each message that asks for or decides something in `thread`, oldest first. A later reply can overrule an earlier request (for example, contracts saying the vendor will be chosen by tender). Fields like `requested_action` describe the original request; the thread captures what happened after.
- `vendor_named` is a vendor the email proposes or names for this work before any tender. If a vendor is named only for part of the work (for example a mock-up floor), still record it, and record that part under `exclusions`.
- Use roles, not personal names, in `requested_by` and `thread[].who` (e.g. "Site QS & Billing", "Contracts & Planning"). Quotes may still contain names because they are verbatim.
- `open_questions`: what the back office must still confirm before it can float a tender, judged only from this email. Quantities are read separately from the attached sheet, so don't list "quantities" as missing when the email says a quantity sheet is attached."""


class IntakeError(RuntimeError):
    pass


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def has_credentials() -> bool:
    return any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")) or (
        Path.home() / ".config" / "anthropic"
    ).exists()


def email_as_prompt(mail: Email) -> str:
    head = "\n".join(
        f"{k}: {v}" for k, v in (("Subject", mail.subject), ("From", mail.sender), ("Date", mail.date)) if v
    )
    names = ", ".join(a.filename for a in mail.attachments) or "none"
    return f"{head}\nAttachments: {names}\n\n{mail.body}"


def extract_live(mail: Email, client: anthropic.Anthropic | None = None) -> ExtractedRequirement:
    client = client or _client()
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            # Server-side fallback: if a safety classifier declines, the API re-runs on another model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[
                {
                    "role": "user",
                    "content": f"<email>\n{email_as_prompt(mail)}\n</email>\n\nExtract the requirement from this email.",
                }
            ],
            output_format=ExtractedRequirement,
        )
    except anthropic.AuthenticationError as e:
        raise IntakeError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise IntakeError("Rate limited by the Anthropic API. Try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise IntakeError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise IntakeError("Couldn't reach the Anthropic API. Check the network.") from e

    if response.stop_reason == "refusal":
        raise IntakeError("The model declined to read this email.")
    if response.stop_reason == "max_tokens":
        raise IntakeError("The extraction ran out of room before finishing.")
    if response.parsed_output is None:
        raise IntakeError("The model's answer didn't match the requirement schema.")
    return response.parsed_output


def extract_replay(path: Path) -> ExtractedRequirement:
    """Load a saved extraction. Used for tests and for demoing the sample without an API key."""
    return ExtractedRequirement.model_validate(json.loads(path.read_text()))


# ── Quote verification ─────────────────────────────────────────────────────────────────────────

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def _norm(s: str) -> str:
    s = s.translate(_QUOTES)
    s = re.sub(r"(?m)^\s*(>\s?)+", "", s)  # quoted-reply markers
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_found(quote: str | None, haystack: str) -> bool:
    if not quote:
        return False
    q = _norm(quote).strip(" .,;:\"'")
    if not q:
        return False
    if q in haystack:
        return True
    # Allow a quote that skips a line break or an ellipsis: every piece must appear in order.
    pieces = [p.strip(" .,;:") for p in re.split(r"\.\.\.|…", q) if p.strip(" .,;:")]
    pos = 0
    for p in pieces:
        idx = haystack.find(p, pos)
        if idx < 0:
            return False
        pos = idx + len(p)
    return len(pieces) > 0


def verify_quotes(req: ExtractedRequirement, mail: Email) -> tuple[ExtractedRequirement, list[DroppedField]]:
    """Drop every field whose quote isn't in the email. Returns the cleaned requirement and what was dropped."""
    haystack = _norm(email_as_prompt(mail))
    dropped: list[DroppedField] = []
    data = req.model_copy(deep=True)

    for name in type(data).model_fields:
        field = getattr(data, name)
        if isinstance(field, Cited) and field.value is not None:
            if not quote_found(field.quote, haystack):
                dropped.append(DroppedField(field=name, value=field.value, quote=field.quote, reason="quote not found in the email"))
                setattr(data, name, Cited(value=None, quote=None))
        elif isinstance(field, CitedList) and field.values:
            if not quote_found(field.quote, haystack):
                dropped.append(DroppedField(field=name, value="; ".join(field.values), quote=field.quote, reason="quote not found in the email"))
                setattr(data, name, CitedList(values=[], quote=None))

    kept = []
    for i, t in enumerate(data.thread):
        if quote_found(t.quote, haystack):
            kept.append(t)
        else:
            dropped.append(DroppedField(field=f"thread[{i}]", value=f"{t.who}: {t.position}", quote=t.quote, reason="quote not found in the email"))
    data.thread = kept
    return data, dropped
