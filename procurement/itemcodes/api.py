"""Item code endpoints (step 2). Included by procurement/api.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import master as M
from .match import RequirementLine, lines_from_record, match_lines

router = APIRouter(prefix="/api/itemcodes", tags=["item codes"])

DEMO_INBOX = M.ROOT / "samples" / "demo_inbox.json"
TITLE = "Match item codes"


def _demo() -> list[dict]:
    p = Path(DEMO_INBOX)
    return json.loads(p.read_text()) if p.exists() else []


def _not_ready(e: M.MasterMissing) -> dict:
    return {"ready": False, "message": str(e)}


def _use_llm(mode: str) -> bool | None:
    return False if mode == "rules" else None


@router.get("/master")
def master() -> dict:
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return _not_ready(e) | {"entries": []}
    rep = M.duplicate_report(m)
    issues: dict[int, list[str]] = {}
    for g in rep.groups:
        for mem in g.members:
            for r in mem.rows:
                issues.setdefault(r, []).append(g.kind)
    for x in rep.misplaced:
        issues.setdefault(x.row, []).append("misplaced")
    return {
        "ready": True, "message": None, "path": m.path, "sheet": m.sheet, "title": m.title,
        "rows": len(m.entries), "codes": len(m.codes()),
        "entries": [e.model_dump(exclude={"tokens"}) | {"issues": sorted(set(issues.get(e.row, [])))} for e in m.entries],
    }


@router.get("/duplicates")
def duplicates() -> dict:
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return _not_ready(e) | {"groups": [], "misplaced": [], "notes": []}
    rep = M.duplicate_report(m)
    return {
        "ready": True, "message": None,
        "counts": {k: rep.count(k) for k in ("repeated", "same_code_different_text", "near_duplicate")} | {"misplaced": len(rep.misplaced)},
        **rep.model_dump(),
    }


@router.get("/match/{uid}")
def match_demo(uid: str, mode: Literal["auto", "rules"] = "auto") -> dict:
    rec = next((r for r in _demo() if str(r.get("uid")) == uid), None)
    if rec is None:
        raise HTTPException(404, "No demo email with that id.")
    email = rec["result"].get("email") or {}
    head = {"uid": uid, "subject": email.get("subject"), "sender": email.get("sender"),
            "from_sheet": bool((rec["result"].get("quantities") or {}).get("lines"))}
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return _not_ready(e) | head | {"lines": []}
    lines = lines_from_record(rec, m)
    return {"ready": True} | head | match_lines(lines, m, use_llm=_use_llm(mode)).model_dump()


class LineIn(BaseModel):
    text: str
    id: str | None = None
    quantity: float | None = None
    uom: str | None = None
    site_code: str | None = None


@router.post("/match")
def match_posted(lines: list[LineIn], mode: Literal["auto", "rules"] = "auto") -> dict:
    if not lines or not any(ln.text.strip() for ln in lines):
        raise HTTPException(400, "Send at least one line with text.")
    if len(lines) > 200:
        raise HTTPException(413, "Send at most 200 lines at a time.")
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return _not_ready(e) | {"lines": []}
    req = [
        RequirementLine(id=ln.id or str(i), text=ln.text.strip(), quantity=ln.quantity, uom=ln.uom, site_code=ln.site_code, source="Posted")
        for i, ln in enumerate(lines, start=1) if ln.text.strip()
    ]
    return {"ready": True} | match_lines(req, m, use_llm=_use_llm(mode)).model_dump()


@router.get("/summary")
def summary() -> dict:
    try:
        m = M.get_master()
    except M.MasterMissing as e:
        return {"title": TITLE, "headline": str(e), "stats": [], "ready": False}
    rep = M.duplicate_report(m)
    kinds = {"exact": 0, "partial": 0, "none": 0}
    for rec in _demo():
        for x in match_lines(lines_from_record(rec, m), m, use_llm=False).lines:
            kinds[x.kind] += 1
    total = sum(kinds.values())
    two = rep.count("same_code_different_text")
    near = rep.count("near_duplicate")
    headline = (
        f"{total} requirement lines checked against {rep.codes} ERP codes: {kinds['exact']} match, "
        f"{kinds['partial']} partly match, {kinds['none']} need a new code. "
        f"{two} codes have two meanings."
    )
    return {
        "title": TITLE,
        "headline": headline,
        "stats": [
            {"label": "Exact match", "value": kinds["exact"], "tone": "good"},
            {"label": "Partial match", "value": kinds["partial"], "tone": "warn" if kinds["partial"] else None},
            {"label": "Need a new code", "value": kinds["none"], "tone": "warn" if kinds["none"] else None},
            {"label": "Codes with two meanings", "value": two, "tone": "bad" if two else "good"},
            {"label": "Likely duplicate groups", "value": near, "tone": "warn" if near else "good"},
        ],
        "ready": True,
    }
