"""HTTP endpoints for email rules: /api/email-rules/…"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from . import rules as R

ROOT = Path(__file__).resolve().parent.parent.parent
DEMO_INBOX = ROOT / "samples" / "demo_inbox.json"

router = APIRouter(prefix="/api/email-rules", tags=["email rules"])


def _err(e: R.RuleError) -> HTTPException:
    return HTTPException(422, str(e))


def _view(r: R.Rule) -> dict:
    return r.model_dump() | {"text": r.describe(), "read_only": r.source == "env"}


# ── Emails to preview against ─────────────────────────────────────────────────────────────────


def _emails() -> list[dict]:
    """Demo emails plus any saved inbox emails, each as {source, uid, subject, sender, facts}."""
    out: list[dict] = []
    try:
        demo = json.loads(DEMO_INBOX.read_text())
    except (OSError, json.JSONDecodeError):
        demo = []
    for rec in demo:
        em = (rec.get("result") or {}).get("email") or {}
        out.append({"source": "demo", "uid": str(rec.get("uid")), "facts": R.EmailFacts.from_dict(em)})

    from .. import mailbox  # late import: mailbox imports this package

    store = mailbox.STORE
    for p in sorted(store.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0) if store.exists() else []:
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        facts = rec.get("facts") or ((rec.get("result") or {}).get("email")) or {"subject": rec.get("subject"), "sender": rec.get("sender")}
        out.append({"source": "inbox", "uid": str(rec.get("uid", p.stem)), "status": rec.get("status"),
                    "facts": R.EmailFacts.from_dict(facts)})
    return out


def _run(rule_set: list[R.Rule], emails: list[dict], baseline: list[R.Rule] | None = None) -> dict:
    rows, counts = [], {"process": 0, "block": 0, "hold": 0}
    for e in emails:
        f: R.EmailFacts = e["facts"]
        d = R.evaluate(rule_set, f)
        counts[d["decision"]] += 1
        row = {"source": e["source"], "uid": e["uid"], "subject": f.subject, "sender": f.sender,
               "attachments": f.attachments, "status": e.get("status")} | d
        if baseline is not None:
            before = R.evaluate(baseline, f)["decision"]
            row |= {"before": before, "changed": before != d["decision"]}
        rows.append(row)
    return {"emails": rows, "counts": counts}


def _current() -> list[R.Rule]:
    try:
        return R.active_rules()
    except R.RuleError as e:
        raise _err(e) from e


# ── Endpoints ─────────────────────────────────────────────────────────────────────────────────


@router.get("")
def list_rules() -> dict:
    try:
        saved = R.load_rules()
    except R.RuleError as e:
        raise _err(e) from e
    return {
        "rules": [_view(r) for r in saved],
        "env_rules": [_view(r) for r in R.env_rules()],
        "how_rules_combine": R.HOW_RULES_COMBINE,
        "fields": R.FIELD_LABELS,
        "matches": R.MATCH_LABELS,
    }


@router.post("", status_code=201)
def create_rule(body: dict[str, Any] = Body(...)) -> dict:
    try:
        return _view(R.add_rule(body))
    except R.RuleError as e:
        raise _err(e) from e


def _by_id(fn, rule_id: str, *args) -> dict | None:
    try:
        out = fn(rule_id, *args)
    except R.RuleError as e:
        raise HTTPException(400 if rule_id.startswith("env-") else 422, str(e)) from e
    except KeyError:
        raise HTTPException(404, "No rule with that id. It may have been deleted.") from None
    return _view(out) if out is not None else None


@router.put("/{rule_id}")
def update_rule(rule_id: str, body: dict[str, Any] = Body(...)) -> dict:
    return _by_id(R.update_rule, rule_id, body)


@router.delete("/{rule_id}")
def delete_rule(rule_id: str) -> dict:
    _by_id(R.delete_rule, rule_id)
    return {"deleted": rule_id}


class ToggleIn(BaseModel):
    enabled: bool | None = None


@router.post("/{rule_id}/toggle")
def toggle_rule(rule_id: str, body: ToggleIn | None = None) -> dict:
    return _by_id(R.toggle_rule, rule_id, body.enabled if body else None)


class PreviewIn(BaseModel):
    """Leave both empty to preview the saved rules.

    rules:    a whole proposed rule set (replaces the saved rules; .env rules still apply).
    draft:    one proposed rule added to the saved rules, or replacing `draft_id` when editing.
    """

    rules: list[dict[str, Any]] | None = None
    draft: dict[str, Any] | None = None
    draft_id: str | None = None


@router.post("/preview")
def preview(body: PreviewIn | None = None) -> dict:
    body = body or PreviewIn()
    current = _current()
    proposed = current
    try:
        if body.rules is not None:
            proposed = [R.new_rule(d) for d in body.rules] + R.env_rules()
        if body.draft is not None:
            d = R.new_rule(body.draft).model_copy(update={"id": body.draft_id or "draft"})
            base = [r for r in proposed if r.id != body.draft_id] if body.draft_id else proposed
            proposed = base + [d]
    except R.RuleError as e:
        raise _err(e) from e
    changed = body.rules is not None or body.draft is not None
    out = _run(proposed, _emails(), baseline=current if changed else None)
    return out | {"proposed": changed, "how_rules_combine": R.HOW_RULES_COMBINE}


@router.get("/summary")
def summary() -> dict:
    try:
        rules = R.active_rules()
    except R.RuleError as e:
        return {"title": "Email rules", "headline": str(e), "stats": [], "ready": False}
    on = [r for r in rules if r.enabled]
    c = _run(rules, _emails())["counts"]
    total = sum(c.values())
    if not on:
        headline = "No rules yet: every email is read."
    else:
        headline = f"{len(on)} rule{'s' if len(on) != 1 else ''} on: {c['process']} of {total} emails would be read."
    return {
        "title": "Email rules",
        "headline": headline,
        "stats": [
            {"label": "Rules on", "value": len(on), "tone": None},
            {"label": "Read", "value": c["process"], "tone": "good"},
            {"label": "Blocked", "value": c["block"], "tone": "bad" if c["block"] else None},
            {"label": "Held", "value": c["hold"], "tone": "warn" if c["hold"] else None},
        ],
        "ready": True,
    }
