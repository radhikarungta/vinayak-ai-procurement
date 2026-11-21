"""The site's answer to our reply: link it to its case, and read it into suggested decisions.

How a reply finds its case (procurement/mailbox.sync calls `match_case` before intake):
1. In-Reply-To / References carry the Message-ID we set on the reply we sent (reply.send keeps it).
2. Otherwise: the subject is our reply's subject ("Re: ..." repeats allowed) AND the sender is the
   address we sent it to. Both must hold, so a new requirement with a similar subject is not swallowed.
A matched email is kept on the case (state["site_replies"]) and never becomes a new requirement.

On a demo case, `simulate` writes the answer a site engineer would plausibly send to the questions we
asked (template text, no model), so the round trip can be shown without a real site inbox.

A reply can also be pasted in by hand (an answer that came by phone or WhatsApp, or a demo case).

Reading it: the model gets the questions we asked (undecided lines with their code options, open email
checks) and the site's new text, and suggests per question: use these codes, leave the line out, the
check is sorted, or unclear. Every suggestion must quote the reply verbatim and may only use a code we
offered for that line or one in the ERP list; anything else becomes "unclear". Nothing is decided
here: a person accepts each suggestion in step 2 (it goes through flow.decide / flow.resolve_check).
"""

from __future__ import annotations

import os
import re
from email.utils import parseaddr
from typing import Any, Literal

from pydantic import BaseModel

from .. import llm
from ..intake.extract import _norm, quote_found
from ..itemcodes import master as M
from . import flow as F

Action = Literal["use_code", "skip", "resolved", "unclear"]


class Answer(BaseModel):
    """What the model reads for one question. All fields required (strict structured output)."""

    ref: str  # the line id, or "check:<id>"
    action: Action
    codes: list[str]  # for use_code: the item codes the site's answer points to
    quote: str  # the words in the reply this rests on, copied exactly ("" when the reply doesn't answer it)
    note: str  # one short line for the buyer, e.g. "floor 80 sqm, wall 40 sqm"


class Answers(BaseModel):
    answers: list[Answer]


class Suggestion(BaseModel):
    ref: str
    action: Action
    codes: list[str] = []
    quote: str = ""
    note: str = ""
    why_unclear: str | None = None  # set when the model's answer was turned into "unclear" by a check


class SiteReply(BaseModel):
    at: str
    sender: str | None
    subject: str | None
    body: str  # the site's new text (the quoted earlier mail cut off)
    via: Literal["email", "pasted", "simulated"]
    message_id: str | None = None
    read: Literal["read", "needs_key", "failed", "nothing_asked"] = "needs_key"
    error: str | None = None
    suggestions: list[Suggestion] = []


SYSTEM = """You read a construction site's reply to questions from the procurement team about a material requirement.

You get the questions (requirement lines whose item code is unclear, each with the code options we offered, and problems with the original email) and the site's reply.

For every question, return one answer with its `ref`:
- use_code: the reply says which item it is. Put the item code(s) in `codes`, only from the options listed for that line, or a code the reply states outright. If the reply splits a line (e.g. floor and wall), give every code and put the quantities in `note`.
- skip: the reply says the line isn't needed, is dropped, or is out of scope.
- resolved: for a "check:" question, the reply gives what was missing (e.g. the quantity, a date, a reason).
- unclear: the reply doesn't answer it, or the answer is ambiguous. Never guess.

`quote`: copy the words from the reply that support the answer, exactly as written, the shortest span that does. A program checks it against the reply and turns the answer into "unclear" if it isn't found. Use "" for unclear.
`note`: one short line for the buyer (quantities, units, conditions). No names of people.
"""


# ── Linking an incoming email to its case ──────────────────────────────────────────────────────


def _ids(header: str | None) -> set[str]:
    return {m.lower() for m in re.findall(r"<[^<>\s]+>", header or "")}


def _subject(s: str | None) -> str:
    s = (s or "").strip()
    while re.match(r"(?i)^(re|fw|fwd)\s*:\s*", s):
        s = re.sub(r"(?i)^(re|fw|fwd)\s*:\s*", "", s, count=1)
    return re.sub(r"\s+", " ", s).lower()


def match_case(*, in_reply_to: str | None, references: str | None, subject: str | None, sender: str | None,
               message_id: str | None = None) -> str | None:
    """The uid of the case whose reply to the site this email answers, or None."""
    ids = _ids(in_reply_to) | _ids(references)
    addr = parseaddr(sender or "")[1].lower()
    own = (os.environ.get("GMAIL_ADDRESS") or "").strip().lower()
    mine = _ids(message_id)
    states = F.all_states()
    if mine and any((r.get("message_id") or "").lower() in mine for _, st in states for r in st.get("replies", [])):
        return None  # our own reply, delivered to a test inbox we also read
    for uid, state in states:
        for r in state.get("replies", []):
            if r.get("message_id") and r["message_id"].lower() in ids:
                return uid
    # No threading headers (some phone clients drop them): only a "Re:" email, from the address we wrote to,
    # with our reply's subject, and never from our own mailbox. A fresh email with the original subject is a
    # new requirement. When several cases fit, the one we wrote to most recently.
    if not addr or addr == own or not re.match(r"(?i)^\s*re\s*:", subject or "") or not _subject(subject):
        return None
    best: tuple[str, str] | None = None
    for uid, state in states:
        for r in state.get("replies", []):
            if r.get("to", "").lower() == addr and _subject(subject) == _subject(r.get("subject")):
                if best is None or r.get("at", "") > best[1]:
                    best = (uid, r.get("at", ""))
    return best[0] if best else None


# ── The site's new text ────────────────────────────────────────────────────────────────────────

_CUT = re.compile(r"(?im)^(on .{4,200}wrote:\s*$|-{2,}\s*original message\s*-{2,}\s*$|from:\s.+$\n^(sent|date):.*$)")
_QUOTED = re.compile(r"^\s*>")


def _inline(lines: list[str]) -> bool:
    """Something typed below a quoted line: answers written under the questions."""
    first = next((i for i, ln in enumerate(lines) if _QUOTED.match(ln)), None)
    return first is not None and any(ln.strip() and not _QUOTED.match(ln) for ln in lines[first:])


def new_text(body: str) -> str:
    """The reply without the earlier mail it quotes. An answer typed under the quoted questions (inline)
    keeps those quoted lines, so each answer still sits under its question."""
    body = body or ""
    m = _CUT.search(body)
    before, after = (body[: m.start()], body[m.end():]) if m else (body, "")
    if _inline(after.splitlines()):
        return (before.rstrip() + "\n\n" + after.strip()).strip()
    if _inline(before.splitlines()):
        return before.strip()
    return "\n".join(ln for ln in before.splitlines() if not _QUOTED.match(ln)).strip()


def own_words(text: str) -> str:
    """Only what the site wrote: quoted lines (our questions) left out. Quotes are checked against this."""
    return "\n".join(ln for ln in (text or "").splitlines() if not _QUOTED.match(ln)).strip()


# ── Reading the reply ──────────────────────────────────────────────────────────────────────────


def questions(case: F.Case) -> tuple[list[F.CaseLine], list[dict]]:
    lines = [ln for ln in case.lines if ln.status == "to_reply"]
    checks = [c for c in case.email_checks if not c.get("resolved")]
    return lines, checks


def offered(ln: F.CaseLine) -> list[tuple[str, str]]:
    m = ln.match
    out = [(m.code, m.erp_text or "")] if m.code else []
    out += [(c, "") for c in m.also]
    out += [(c.code, c.text) for c in m.candidates if c.code not in {x for x, _ in out}]
    if m.suggestion:
        out.append((m.suggestion.code, "a new code we'd open"))
    return out


def prompt(case: F.Case, text: str) -> str:
    lines, checks = questions(case)
    parts = ["<questions>"]
    for ln in lines:
        qty = f" ({' '.join(x for x in (f'{ln.quantity:g}', ln.uom or '') if x)})" if ln.quantity is not None else ""
        parts.append(f"ref: {ln.id}\nline: {ln.text}{qty}\nproblem: " + " ".join(i.text for i in ln.issues))
        parts.append("options: " + ("; ".join(f"{c} \"{t}\"" if t else c for c, t in offered(ln)) or "none"))
        parts.append("")
    for c in checks:
        parts.append(f"ref: check:{c['id']}\nproblem with the email: {c['title']}. {c['detail']}\n")
    parts += ["</questions>", "", "<reply>", text, "</reply>", "", "Answer every question."]
    return "\n".join(parts)


def verify(case: F.Case, text: str, answers: list[Answer], known: set[str]) -> list[Suggestion]:
    """One suggestion per question, in question order. The model's answer stands only if its quote is in
    the reply and its codes are ones we offered or in the ERP list."""
    lines, checks = questions(case)
    by_ref = {a.ref: a for a in answers}
    hay = _norm(own_words(text))
    canon = {c.strip().upper(): c for c in known}
    out = []
    for ref, ln in [(ln.id, ln) for ln in lines] + [(f"check:{c['id']}", None) for c in checks]:
        a = by_ref.get(ref)
        if a is None or a.action == "unclear":
            out.append(Suggestion(ref=ref, action="unclear", note=a.note if a else ""))
            continue
        s = Suggestion(ref=ref, action=a.action, codes=[c.strip() for c in a.codes if c.strip()], quote=a.quote, note=a.note)
        if ln is not None:  # "wp.01" from the model is WP.01: map to the code as written in our list
            mine = canon | {c.strip().upper(): c for c, _ in offered(ln)}
            s.codes = [mine.get(c.upper(), c) for c in s.codes]
        bad = None
        if not quote_found(a.quote, hay):
            bad = "The words it relied on aren't in the reply."
        elif ln is None and a.action != "resolved":
            bad = "Only \"sorted\" applies to a problem with the email."
        elif ln is not None and a.action == "resolved":
            bad = "A line needs a code or to be left out."
        elif a.action == "use_code":
            allowed = {c for c, _ in offered(ln)} | known
            wrong = [c for c in s.codes if c not in allowed]
            if not s.codes:
                bad = "No item code given."
            elif wrong:
                bad = f"Not an offered or ERP code: {', '.join(wrong)}."
        if bad:
            s = Suggestion(ref=ref, action="unclear", quote=a.quote, note=a.note, why_unclear=bad)
        if s.action != "use_code":
            s.codes = []
        out.append(s)
    return out


def read(case: F.Case, text: str, *, client: Any = None, master: M.Master | None = None) -> tuple[str, str | None, list[Suggestion]]:
    """(read status, error, suggestions)."""
    lines, checks = questions(case)
    if not (lines or checks):
        return "nothing_asked", None, []
    if client is None and not llm.ready():
        return "needs_key", "Not read yet: set OPENAI_API_KEY, then press Read again.", []
    try:
        got = llm.parse(client or llm.client(), text_format=Answers, instructions=SYSTEM, user=prompt(case, text),
                        max_output_tokens=4000, effort="low")
    except llm.LLMError as e:
        return "failed", str(e), []
    known = (master or M.get_master()).codes()
    return "read", None, verify(case, text, got.answers, known)


# ── Keeping it on the case ─────────────────────────────────────────────────────────────────────


def record(uid: str, body: str, *, sender: str | None = None, subject: str | None = None, via: str = "pasted",
           message_id: str | None = None, at: str | None = None, client: Any = None, master: M.Master | None = None,
           fallback: list[Suggestion] | None = None) -> SiteReply:
    m = master or M.get_master()
    case = F.build_case(uid, master=m)
    text = new_text(body)
    if not own_words(text):
        raise F.CaseError("The reply is empty.")
    status, err, sugg = read(case, text, client=client, master=m)
    if fallback is not None and status in ("needs_key", "failed"):  # simulated: we know what it says
        status, err, sugg = "read", f"Read without the model ({err.split(':')[0] if err else 'no key'}): the demo answer's own mapping.", fallback
    r = SiteReply(at=at or F.now(), sender=sender, subject=subject, body=text[:20000], via=via, message_id=message_id,
                  read=status, error=err, suggestions=sugg)
    state = F.load_state(uid)
    state.setdefault("site_replies", []).append(r.model_dump())
    usable = sum(s.action != "unclear" for s in sugg)
    F.log_event(state, "site_reply", f"The site answered ({ {'email': 'by email', 'pasted': 'pasted in', 'simulated': 'simulated'}[via] }): "
                f"{usable} of {len(sugg)} question{'s' * (len(sugg) != 1)} answered clearly." if status == "read" else
                f"The site answered ({via}); not read yet: {err or status}.", actor="site")
    F.save_state(uid, state)
    return r


def reread(uid: str, index: int, *, client: Any = None, master: M.Master | None = None) -> SiteReply:
    """Read a kept reply again (after a key was set, or against the questions still open now)."""
    m = master or M.get_master()
    replies = F.load_state(uid).get("site_replies", [])
    if not 0 <= index < len(replies):
        raise F.CaseError("No such reply from the site.")
    r = SiteReply(**replies[index])
    r.read, r.error, r.suggestions = read(F.build_case(uid, master=m), r.body, client=client, master=m)
    state = F.load_state(uid)  # again after the model call: a decision made meanwhile must not be overwritten
    if index >= len(state.get("site_replies", [])):
        raise F.CaseError("No such reply from the site.")
    state["site_replies"][index] = r.model_dump()
    F.save_state(uid, state)
    return r


# ── Demo: the site's answer, simulated ─────────────────────────────────────────────────────────


def _num(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


FACTS = {"finish date": "Finish date is 15 Dec 2026", "start date": "Work starts 1 Dec 2026",
         "delivery": "Deliver to the site store, Gate 2", "quantity": "Quantities are in the attached sheet"}


def _check_answer(c: dict) -> str:
    """The missing fact the check asks for, stated plainly (demo)."""
    text = f"{c.get('title', '')} {c.get('detail', '')}".lower()
    if "sheet" in text or "attach" in text:
        return "Resending the quantity sheet as .xlsx with this mail."
    m = re.search(r"not stated:\s*([^.]+)", text)
    asked = [a.strip() for a in re.split(r",|\band\b", m.group(1))] if m else []
    facts = [FACTS.get(a, f"{a.capitalize()}: as per the drawing") for a in asked if a]
    return (". ".join(facts) + ".") if facts else f"Noted on \"{c.get('title', '').lower()}\": details as in the sheet."


def simulated_answer(case: F.Case) -> tuple[str, list[Suggestion]]:
    """A plausible answer from the site to every open question, and what it means (used when no model
    is set). Partial matches are split 60/40, a conflict takes the code the line names, an unmatched line
    gets the suggested new code, and each email problem gets the missing fact."""
    lines, checks = questions(case)
    out = ["Hi,", "", "Answers to your points:", ""]
    sugg: list[Suggestion] = []
    n = 0
    for ln in lines:
        n += 1
        m, kinds = ln.match, [i.kind for i in ln.issues]
        uom = f" {ln.uom.lower()}" if ln.uom else ""
        if "partial" in kinds and m.code and m.also and "conflict" not in kinds:
            codes = [m.code, *m.also]
            if ln.quantity:
                a = round(ln.quantity * 0.6)
                said = f"It's both. {_num(a)}{uom} under {codes[0]} and {_num(ln.quantity - a)}{uom} under {', '.join(codes[1:])}."
            else:
                said = f"It's both, take it under {' and '.join(codes)}."
            sugg.append(Suggestion(ref=ln.id, action="use_code", codes=codes, quote=said, note=said))
        elif m.code and ("conflict" in kinds or "partial" in kinds):
            said = f"Use {m.code} ({(m.erp_text or '').lower()}), that's what we meant."
            sugg.append(Suggestion(ref=ln.id, action="use_code", codes=[m.code], quote=said, note=""))
        elif m.suggestion:
            said = f"There's no code for it yet; the specification is as written in the sheet. Please open new code {m.suggestion.code}."
            sugg.append(Suggestion(ref=ln.id, action="use_code", codes=[m.suggestion.code], quote=f"Please open new code {m.suggestion.code}.",
                                   note=f"New code {m.suggestion.code}: {ln.text}"))
        elif m.candidates:
            c = m.candidates[0]
            said = f"Go with {c.code} ({c.text.lower()})."
            sugg.append(Suggestion(ref=ln.id, action="use_code", codes=[c.code], quote=said, note=""))
        else:
            said = "Leave this one out, we'll buy it locally."
            sugg.append(Suggestion(ref=ln.id, action="skip", quote=said, note="Bought locally by the site."))
        out += [f"{n}. {ln.text}: {said}", ""]
    for c in checks:
        n += 1
        said = _check_answer(c)
        sugg.append(Suggestion(ref=f"check:{c['id']}", action="resolved", quote=said, note=said))
        out += [f"{n}. {c['title']}: {said}", ""]
    out += ["Thanks,", "Site QS"]
    return "\n".join(out), sugg


def simulate(uid: str, *, client: Any = None, master: M.Master | None = None) -> SiteReply:
    """Demo only: the site's answer to our last reply arrives (read by the model when a key is set)."""
    if not uid.isdigit():
        raise F.CaseError("The simulated answer is for demo emails only. Real answers come in by email.")
    m = master or M.get_master()
    case = F.build_case(uid, master=m)
    if not case.replies:
        raise F.CaseError("Send (or mark as sent) the reply to the site first.")
    if not any(questions(case)):
        raise F.CaseError("Nothing is open to answer.")
    body, sugg = simulated_answer(case)
    last = case.replies[-1]
    return record(uid, body, sender=case.sender, subject=last.subject if last.subject.lower().startswith("re:") else f"Re: {last.subject}",
                  via="simulated", client=client, master=m, fallback=sugg)
