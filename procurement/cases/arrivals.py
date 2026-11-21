"""Demo: a new requirement email lands, and the agent takes it from there on its own.

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


def arrive() -> dict:
    """A new requirement email lands. Returns its record (uid, subject, …)."""
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
    rec = {"uid": uid, "received": when, "result": res, "arrived": F.now(), "copied_from": str(tpl["uid"])}
    store().mkdir(parents=True, exist_ok=True)
    (store() / f"{uid}.json").write_text(json.dumps(rec, indent=2))
    state = F.load_state(uid)
    F.log_event(state, "email_in", f"New requirement email landed: {res['email'].get('subject')} (demo).", actor="system")
    F.save_state(uid, state)
    return copy.deepcopy(rec)
