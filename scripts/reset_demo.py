"""Reset the demo: every requirement email at a different point, so the overview tells the whole story.

    uv run python scripts/reset_demo.py          # asks before clearing
    uv run python scripts/reset_demo.py --yes

Clears the saved state of every requirement (data/cases/*.json) and the simulated arrivals
(data/cases/inbox/), then moves each demo email (samples/demo_inbox.json) to its own stage. Some are
moved by a person (the history says "you"), some by the agent, so both show. Simulated site answers use
the OpenAI key from .env; sends are only marked as sent, nothing is emailed.

  1006 TMT bars            untouched: an email problem to sort (step 2)
  1003 Tile adhesive       reply sent to the site, waiting on its answer (try "Simulate the site's reply")
  1009 Nozzle grouting     items to approve (step 3)
  1010 Roof waterproofing  vendors to pick (step 4)
  1011 Toilet waterproofing RFQs drafted, to send (step 4)
  1008 OPC cement          quotes in, vendor to choose (step 5)
  1004 Scaffolding         the agent ran 7 steps, stopped on a close call (quotes 0.4% apart): you choose
  + new: HOD email sent, waiting on the HOD
  + new: HOD approved
  + new: "checkpoints" run, the agent stopped for you to approve the items
  + new: the agent ran it end to end: HOD email ready (step 6)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from procurement.cases import agent as A  # noqa: E402
from procurement.cases import arrivals as AR  # noqa: E402
from procurement.cases import db as D  # noqa: E402
from procurement.cases import flow as F  # noqa: E402
from procurement.cases import quotes as CQ  # noqa: E402


def drive(uid: str, target: str, actor: str = "person") -> str:
    """Move a requirement on, one step at a time (the agent's own moves), until it reaches `target`."""
    run = A.Run(at=F.now(), mode="demo")
    for _ in range(20):
        c = F.build_case(uid)
        if c.stage == target:
            return c.stage
        with F.acting_as(actor), F.STATE_LOCK:
            A._move(c, True, run, None)
    raise SystemExit(f"{uid} never reached {target} (stuck at {F.build_case(uid).stage})")


def send_hod(uid: str) -> None:
    d = CQ.view(F.build_case(uid))["hod_draft"]
    CQ.send_hod(uid, d["to"] or "hod@demo.invalid", d["subject"], d["body"], mark_only=True)


def main() -> None:
    if "--yes" not in sys.argv and input("Clear every requirement's saved state and the simulated emails? [y/N] ").lower() != "y":
        return
    d = F.cases_dir()
    for p in d.glob("*.json"):
        p.unlink()
    shutil.rmtree(AR.store(), ignore_errors=True)
    if D.enabled():  # the hosted app's data (SUPABASE_URL / SUPABASE_SERVICE_KEY set)
        D.delete_all("case_state")
        D.delete_all("arrivals")

    plan = [("1009", "approve", "person"), ("1010", "vendors", "person"), ("1011", "rfq", "person"),
            ("1008", "quotes", "person"), ("1003", "waiting_on_site", "person")]
    for uid, target, actor in plan:
        print(f"{uid}: {drive(uid, target, actor)}")
    print("1004:", A.run("1004", delay=0).reason)  # the agent stops on a close call between two quotes
    print("1006: untouched")

    a = AR.arrive("auto")["uid"]  # sent to the HOD, waiting
    A.run(a, delay=0)
    send_hod(a)
    print(f"{a}: HOD email sent")
    b = AR.arrive("auto")["uid"]  # approved by the HOD
    A.run(b, delay=0)
    send_hod(b)
    with F.acting_as("person"):
        CQ.approve_hod(b, True)
    print(f"{b}: HOD approved")
    c = AR.arrive("checkpoints")["uid"]  # the agent stops for a person
    print(f"{c}:", A.run(c, delay=0).reason)
    e = AR.arrive("auto")["uid"]  # the agent end to end: HOD email drafted
    print(f"{e}:", A.run(e, delay=0).stopped)


if __name__ == "__main__":
    main()
