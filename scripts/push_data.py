"""Copy the local data to Supabase for the hosted app (re-runnable; replaces what's there).

    uv run python scripts/push_data.py

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment (or pass them inline; keep them out of
.env if the local app should stay on files). Sends:
  - data/ (vendors, deals, seed, tender folder; not cases/ or backups) as one private Storage object;
  - every requirement's saved state (data/cases/*.json) into case_state;
  - the simulated emails (data/cases/inbox/*.json) into arrivals.
Real client data goes only to this private Supabase project, never the repo.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from procurement import cloud  # noqa: E402
from procurement.cases import db as D  # noqa: E402


def bundle() -> bytes:
    buf = io.BytesIO()
    skip = lambda p: p.parts[1:2] == ("cases",) or ".bak" in p.name or p.parts[1:2] == ("inbox",)  # noqa: E731
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in sorted((ROOT / "data").rglob("*")):
            rel = p.relative_to(ROOT)
            if p.is_file() and not skip(rel) and p.name != ".DS_Store":
                t.add(p, arcname=str(rel))
    return buf.getvalue()


def main() -> None:
    if not D.enabled():
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY first.")
    b = bundle()
    cloud.upload(b)
    print(f"data bundle: {len(b) / 1e6:.1f} MB")
    cases = ROOT / "data" / "cases"
    D.delete_all("case_state")
    D.delete_all("arrivals")
    for p in sorted(cases.glob("*.json")):
        D.save(p.stem, json.loads(p.read_text()))
    for p in sorted((cases / "inbox").glob("*.json")):
        D.save(p.stem, json.loads(p.read_text()), table="arrivals", col="rec")
    print(f"case states: {len(list(cases.glob('*.json')))} · simulated emails: {len(list((cases / 'inbox').glob('*.json')))}")


if __name__ == "__main__":
    main()
