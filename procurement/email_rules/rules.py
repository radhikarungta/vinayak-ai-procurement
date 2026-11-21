"""Email allow/block rules: decide which inbox emails get read, before any model call.

A rule looks at one part of an email (field) and tests it (match) against a value. All matching
ignores case.

    field            what it looks at
    sender           the sender's address ("contains" and "regex" also see the display name)
    sender_domain    the part of the sender's address after "@"
    subject          the subject line
    body             the email text
    to               every To and Cc address
    has_attachment   "yes" or "no" (match is ignored)
    attachment_type  each attachment's file extension, e.g. xlsx or pdf

    match            contains | equals | starts_with | regex (Python `re.search`)

How rules combine: an enabled block rule that matches always skips the email; otherwise, if
there are any enabled allow rules, the email must match at least one of them or it is held;
with no allow rules, every email that isn't blocked is read.

`evaluate` returns {"decision": "process" | "block" | "hold", "rule_id": str | None,
"reason": plain words}.

Rules are stored in data/email_rules.json (override with EMAIL_RULES_PATH). The WATCH_FROM env
setting still works: each entry acts as an enabled "sender contains <entry>" allow rule, shown in
the UI as read-only "from .env".
"""

from __future__ import annotations

import email
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = ROOT / "data" / "email_rules.json"

HOW_RULES_COMBINE = (
    "Block rules always win; if you have any allow rules, an email must match one of them or it is held; "
    "with no allow rules, every email that isn't blocked is read."
)

Action = Literal["allow", "block"]
FieldName = Literal["sender", "sender_domain", "subject", "body", "has_attachment", "attachment_type", "to"]
Match = Literal["contains", "equals", "starts_with", "regex"]

FIELD_LABELS = {
    "sender": "sender",
    "sender_domain": "sender domain",
    "subject": "subject",
    "body": "email text",
    "has_attachment": "has an attachment",
    "attachment_type": "attachment type",
    "to": "sent to",
}
MATCH_LABELS = {"contains": "contains", "equals": "is", "starts_with": "starts with", "regex": "matches pattern"}
_YES, _NO = {"yes", "true", "1", "y"}, {"no", "false", "0", "n"}


class RuleError(ValueError):
    """A rule that can't be saved, with a message to show the user."""


class RuleIn(BaseModel):
    """What the user sends to create or change a rule."""

    action: Action
    field: FieldName
    match: Match = "contains"
    value: str = Field(max_length=500)
    enabled: bool = True
    note: str = Field(default="", max_length=300)

    @field_validator("value", "note")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check(self) -> "RuleIn":
        if self.field == "has_attachment":
            v = self.value.lower()
            if v not in _YES | _NO:
                raise ValueError('For "has an attachment", the value must be yes or no.')
            self.value, self.match = ("yes" if v in _YES else "no"), "equals"
            return self
        if not self.value:
            raise ValueError("Type a value for the rule to look for.")
        if self.field == "attachment_type":
            self.value = self.value.lstrip(".")
        if self.match == "regex":
            try:
                re.compile(self.value, re.I)
            except re.error as e:
                raise ValueError(f"The pattern isn't valid: {e}. Check brackets and backslashes, or use \"contains\" instead.") from None
        return self


class Rule(RuleIn):
    id: str
    created: str
    source: Literal["saved", "env"] = "saved"

    def describe(self) -> str:
        if self.field == "has_attachment":
            return "has an attachment" if self.value == "yes" else "has no attachment"
        return f'{FIELD_LABELS[self.field]} {MATCH_LABELS[self.match]} "{self.value}"'


def validate(data: dict) -> RuleIn:
    """Check a proposed rule; raises RuleError with a plain message."""
    from pydantic import ValidationError

    try:
        return RuleIn.model_validate(data)
    except ValidationError as e:
        raise RuleError(_plain(e)) from None


def _plain(e) -> str:
    msgs = []
    for err in e.errors():
        msg = err["msg"].removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err["loc"])
        msgs.append(f"{loc}: {msg}" if loc and not msg[:1].isupper() else msg)
    return " ".join(msgs)


# ── What a rule looks at ──────────────────────────────────────────────────────────────────────


@dataclass
class EmailFacts:
    sender: str = ""  # full From header, e.g. "Site QS <qs@site.com>"
    subject: str = ""
    body: str = ""
    to: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)  # filenames

    @property
    def address(self) -> str:
        return parseaddr(self.sender)[1].lower() or self.sender.strip().lower()

    @property
    def domain(self) -> str:
        return self.address.rpartition("@")[2] if "@" in self.address else ""

    def as_dict(self) -> dict:
        return {"sender": self.sender, "subject": self.subject, "body": self.body, "to": self.to, "attachments": self.attachments}

    @classmethod
    def from_dict(cls, d: dict) -> "EmailFacts":
        atts = [a if isinstance(a, str) else a.get("filename", "") for a in d.get("attachments") or []]
        to = d.get("to") or []
        if isinstance(to, str):
            to = [a for _, a in getaddresses([to]) if a]
        return cls(sender=d.get("sender") or "", subject=d.get("subject") or "", body=d.get("body") or "", to=list(to), attachments=atts)

    @classmethod
    def from_email(cls, mail, raw: bytes | str | None = None) -> "EmailFacts":
        """From a parsed intake Email; `raw` (the RFC 822 bytes) adds the To/Cc addresses."""
        return cls(
            sender=mail.sender or "",
            subject=mail.subject or "",
            body=mail.body or "",
            to=_recipients(raw) if raw is not None else [],
            attachments=[a.filename for a in mail.attachments],
        )


def _recipients(raw: bytes | str) -> list[str]:
    try:
        msg = email.message_from_bytes(raw, policy=policy.default) if isinstance(raw, bytes) else email.message_from_string(raw, policy=policy.default)
        heads = [str(h) for name in ("To", "Cc") for h in (msg.get_all(name) or [])]
        return [a.lower() for _, a in getaddresses(heads) if a]
    except Exception:
        return []


def _test(match: str, value: str, target: str) -> bool:
    t, v = target.lower(), value.lower()
    if match == "contains":
        return v in t
    if match == "equals":
        return t.strip() == v
    if match == "starts_with":
        return t.lstrip().startswith(v)
    try:
        return re.search(value, target, re.I) is not None
    except re.error:
        return False


def matches(rule: RuleIn, f: EmailFacts) -> bool:
    if rule.field == "has_attachment":
        return bool(f.attachments) == (rule.value == "yes")
    if rule.field == "attachment_type":
        exts = [n.rpartition(".")[2] for n in f.attachments if "." in n]
        return any(_test(rule.match, rule.value, e) for e in exts)
    if rule.field == "sender":
        targets = [f.address] + ([f.sender] if rule.match in ("contains", "regex") else [])
        return any(_test(rule.match, rule.value, t) for t in targets if t)
    if rule.field == "sender_domain":
        return bool(f.domain) and _test(rule.match, rule.value, f.domain)
    if rule.field == "to":
        return any(_test(rule.match, rule.value, a) for a in f.to)
    return _test(rule.match, rule.value, f.subject if rule.field == "subject" else f.body)


def evaluate(rules: list[Rule], f: EmailFacts) -> dict:
    """Block wins; then, if any allow rules are on, one must match (else hold); else process."""
    on = [r for r in rules if r.enabled]
    for r in on:
        if r.action == "block" and matches(r, f):
            return {"decision": "block", "rule_id": r.id, "reason": f"Blocked: {r.describe()}."}
    allows = [r for r in on if r.action == "allow"]
    if not allows:
        return {"decision": "process", "rule_id": None, "reason": "Read: no rule stops it."}
    for r in allows:
        if matches(r, f):
            return {"decision": "process", "rule_id": r.id, "reason": f"Read: {r.describe()}."}
    return {"decision": "hold", "rule_id": None, "reason": "Held: no allow rule matched."}


# ── Storage ───────────────────────────────────────────────────────────────────────────────────


def rules_path() -> Path:
    return Path(os.environ.get("EMAIL_RULES_PATH") or DEFAULT_PATH)


def load_rules() -> list[Rule]:
    p = rules_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
    except json.JSONDecodeError as e:
        raise RuleError(f"{p.name} isn't valid JSON ({e}). Fix or delete the file.") from None
    return [Rule.model_validate(r) for r in (data.get("rules", []) if isinstance(data, dict) else data)]


def save_rules(rules: list[Rule]) -> None:
    p = rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"rules": [r.model_dump(exclude={"source"}) for r in rules if r.source == "saved"]}, indent=2))
    tmp.replace(p)


def env_rules(watch_from: list[str] | None = None) -> list[Rule]:
    """WATCH_FROM entries as read-only sender allow rules."""
    if watch_from is None:
        watch_from = [w.strip() for w in os.environ.get("WATCH_FROM", "").split(",") if w.strip()]
    return [
        Rule(id=f"env-{i}", action="allow", field="sender", match="contains", value=w, enabled=True,
             note="From WATCH_FROM in .env", created="", source="env")
        for i, w in enumerate(watch_from, 1)
    ]


def active_rules(watch_from: list[str] | None = None) -> list[Rule]:
    return load_rules() + env_rules(watch_from)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_rule(data: dict) -> Rule:
    return Rule(**validate(data).model_dump(), id=uuid.uuid4().hex[:10], created=_now())


def add_rule(data: dict) -> Rule:
    rule = new_rule(data)
    save_rules(load_rules() + [rule])
    return rule


def _find(rules: list[Rule], rule_id: str) -> int:
    if rule_id.startswith("env-"):
        raise RuleError("This rule comes from WATCH_FROM in .env. Change it there and restart.")
    for i, r in enumerate(rules):
        if r.id == rule_id:
            return i
    raise KeyError(rule_id)


def update_rule(rule_id: str, data: dict) -> Rule:
    rules = load_rules()
    i = _find(rules, rule_id)
    rules[i] = Rule(**validate(data).model_dump(), id=rules[i].id, created=rules[i].created)
    save_rules(rules)
    return rules[i]


def toggle_rule(rule_id: str, enabled: bool | None = None) -> Rule:
    rules = load_rules()
    i = _find(rules, rule_id)
    rules[i] = rules[i].model_copy(update={"enabled": (not rules[i].enabled) if enabled is None else enabled})
    save_rules(rules)
    return rules[i]


def delete_rule(rule_id: str) -> None:
    rules = load_rules()
    i = _find(rules, rule_id)
    del rules[i]
    save_rules(rules)
