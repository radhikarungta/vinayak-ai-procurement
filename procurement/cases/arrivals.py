"""Demo: a new requirement email lands, and the agent takes it from there (or a person does).

Three ways to run it (MODES): "auto" the agent runs it end to end; "manual" the agent doesn't start,
a person works it; "checkpoints" the agent runs but stops for a person before approving the items and
before choosing the vendor (agent.py reads the mode from the record), then carries on when run again.

Each new email is one of the demo requirement emails (samples/demo_inbox.json) sent again for another
site: the project and tower are swapped for the next name below, quantities are scaled, it gets the next
free id and today's date. It is kept in <cases dir>/inbox/<uid>.json (git-ignored data, never in the
repo) and read by flow.records() like any demo email, so simulated answers and quotes work on it.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from . import flow as F

# Which demo emails are sent again, in turn (each needs a different path through the flow).
TEMPLATES = ["1003", "1010", "1008", "1006", "1009", "1004", "1011"]
SITES = [("Hillcrest", "Tower-E"), ("Bayview", "Tower-2"), ("Oakridge", "Block-A"), ("Sunfield", "Tower-5"),
         ("Maple Court", "Tower-B"), ("Northgate", "Tower-3")]
PROJECTS = ["Greenfield", "Lakeview", "Riverside"]
MODES = ("auto", "manual", "checkpoints")
CHECKPOINTS = ("approve", "award")  # where a "checkpoints" run stops for a person


def store() -> Path:
    return F.cases_dir() / "inbox"


def records() -> list[dict]:
    d = store()
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []


def _scale(res: dict, k: float) -> None:
    for ln in ((res.get("quantities") or {}).get("lines") or []):
        for f in ("total_sqm", "total_sqft"):
            if isinstance(ln.get(f), int | float):
                ln[f] = round(ln[f] * k, 2)


def checkpoints(uid: str) -> tuple[str, ...]:
    """Where the agent must stop for a person on this requirement (only emails that landed in "checkpoints" mode)."""
    try:
        return CHECKPOINTS if F.record(uid).get("demo_mode") == "checkpoints" else ()
    except F.CaseError:
        return ()


def arrive(mode: str = "auto") -> dict:
    """A new requirement email lands. Returns its record (uid, subject, …)."""
    if mode not in MODES:
        raise F.CaseError(f"Unknown mode {mode}: one of {', '.join(MODES)}.")
    demo = {str(r["uid"]): r for r in json.loads(Path(F.DEMO_INBOX).read_text())} if Path(F.DEMO_INBOX).exists() else {}
    have = records()
    n = len(have)
    order = [t for t in TEMPLATES if t in demo] or list(demo)  # another inbox (tests): its emails in turn
    tpl = demo[order[n % len(order)]] if order else None
    if tpl is None:
        raise F.CaseError("No demo email to send again.")
    uids = [int(str(r["uid"])) for r in [*demo.values(), *have] if str(r["uid"]).isdigit()]
    uid = str(max(uids, default=1000) + 1)
    site, tower = SITES[n % len(SITES)]
    text = json.dumps(tpl["result"])
    for p in PROJECTS:  # the project name, wherever it appears (subject, body, quotes, sender)
        text = text.replace(p, site).replace(p.lower(), re.sub(r"\W+", "", site.lower()))
    text = re.sub(r"(Tower|Block)-[A-Z0-9]+", tower, text)
    res = json.loads(text)
    _scale(res, 1 + 0.15 * ((n % 4) + 1))
    when = format_datetime(datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(microsecond=0))
    res["email"]["date"] = when
    rec = {"uid": uid, "received": when, "result": res, "arrived": F.now(), "copied_from": str(tpl["uid"]), "demo_mode": mode}
    store().mkdir(parents=True, exist_ok=True)
    (store() / f"{uid}.json").write_text(json.dumps(rec, indent=2))
    state = F.load_state(uid)
    how = {"auto": "the agent runs it", "manual": "for a person to work", "checkpoints": "the agent runs it, stopping for a person to approve and to choose"}[mode]
    F.log_event(state, "email_in", f"New requirement email landed: {res['email'].get('subject')} (demo: {how}).", actor="system")
    F.save_state(uid, state)
    return copy.deepcopy(rec)
