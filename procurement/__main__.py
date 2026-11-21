"""CLI:  uv run python -m procurement path/to/email.eml [--attach sheet.xlsx] [--replay extraction.json]

Prints the IntakeResult as JSON.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .intake.email_parser import RawAttachment
from .intake.extract import IntakeError
from .intake.pipeline import load_email, run_intake


def main() -> int:
    ap = argparse.ArgumentParser(prog="procurement", description="Read a site requirement email into a structured requirement + BOQ checks.")
    ap.add_argument("email", type=Path, help=".eml file, or a .txt of pasted email text")
    ap.add_argument("--attach", type=Path, action="append", default=[], help="extra attachment (e.g. the quantity sheet for a .txt email)")
    ap.add_argument("--replay", type=Path, help="use a saved extraction JSON instead of calling the model")
    a = ap.parse_args()

    extra = [RawAttachment(p.name, "application/octet-stream", p.read_bytes()) for p in a.attach]
    mail = load_email(a.email.read_bytes(), extra)
    try:
        result = run_intake(mail, replay=a.replay)
    except IntakeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
