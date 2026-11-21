"""Match requirement lines to ERP item codes.

Rules first, always: each line is compared with every distinct (code, text) in the master on
shared words (after the synonym map in master.py) and gets a shortlist, a kind and a reason.

- exact:   the line and the ERP text describe the same work.
- partial: they overlap, but one is wider or narrower (widen the ERP text, add a variant, or use
           two codes, e.g. toilet floor + toilet wall).
- none:    nothing close; a new code is suggested on the nearest code pattern and checked unused.

When an Anthropic API key is set, Claude may choose among the shortlist and write the reason.
It can only pick codes from the shortlist; anything else falls back to the rule-based answer.
A person makes the final call either way.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

from pydantic import BaseModel, Field

from ..intake import extract
from . import master as M

EXACT_MIN = 0.45  # weighted overlap needed for any match
SITE_SLACK = 0.15  # the site sheet's own code wins if it is this close to the best

# Words that make a line waterproofing work. The master only holds waterproofing codes.
WATERPROOFING_WORDS = {
    "WATERPROOFING", "GROUTING", "COBA", "PUNNING", "SCREED", "EXPANSION", "SEALANT", "DEPRESSION",
    "COMBIFLEX", "POLYSULPHIDE", "HYPALON", "NITOCOTE",
}

# New group prefixes for work that has no group in this master yet.
GROUP_PREFIX = {
    "CEMENT": "CEM", "OPC": "CEM", "PPC": "CEM", "TMT": "TMT", "STEEL": "STL", "REBAR": "TMT",
    "SCAFFOLDING": "SCF", "ADHESIVE": "ADH", "TILE": "TIL", "GRANITE": "GRN", "PAVER": "PAV",
}


class RequirementLine(BaseModel):
    id: str
    text: str
    quantity: float | None = None
    uom: str | None = None
    site_code: str | None = None
    source: str | None = None
    detail: str | None = None


class Candidate(BaseModel):
    code: str
    text: str
    rows: list[int]
    score: float
    common: list[str]
    line_only: list[str]
    erp_only: list[str]
    conflict: bool
    misplaced: bool


class Suggestion(BaseModel):
    code: str
    why: str


class Flag(BaseModel):
    tone: Literal["good", "warn", "bad"] | None
    text: str


class LineMatch(BaseModel):
    line: RequirementLine
    kind: Literal["exact", "partial", "none"]
    code: str | None
    erp_text: str | None
    erp_rows: list[int]
    also: list[str] = []
    reason: str
    suggestion: Suggestion | None = None
    flags: list[Flag] = []
    candidates: list[Candidate]
    decided_by: Literal["rules", "claude"] = "rules"


class MatchResult(BaseModel):
    mode: Literal["rules", "claude"]
    model: str | None
    message: str | None
    lines: list[LineMatch]


# ── Shortlist ──────────────────────────────────────────────────────────────────────────────────


class _Index:
    """Distinct (code, text) nodes of the master, with what the duplicate report says about them."""

    def __init__(self, master: M.Master):
        self.master = master
        self.codes = master.codes()
        self.report = M.duplicate_report(master)
        nodes: dict[tuple[str, str], list[M.Entry]] = {}
        for e in master.entries:
            nodes.setdefault((e.code, e.normalised), []).append(e)
        self.nodes = nodes
        self.conflicts = {g.codes[0]: g for g in self.report.groups if g.kind == "same_code_different_text"}
        self.misplaced_rows = {m.row for m in self.report.misplaced}
        self.vocab = {t for e in master.entries for t in e.tokens if M.weight(t) == 3}


_index_cache: dict[int, _Index] = {}


def _index(master: M.Master) -> _Index:
    key = id(master)
    if key not in _index_cache:
        _index_cache.clear()
        _index_cache[key] = _Index(master)
    return _index_cache[key]


def _w(toks) -> int:
    return sum(M.weight(t) for t in toks)


def shortlist(line_tokens: set[str], idx: _Index, limit: int = 5) -> list[Candidate]:
    out = []
    for (code, _), entries in idx.nodes.items():
        e = entries[0]
        etoks = set(e.tokens)
        ctx = M.code_tokens(code)
        common = line_tokens & (etoks | ctx)
        union = line_tokens | etoks
        score = _w(common) / _w(union) if _w(union) else 0.0
        if not _w(common):
            continue
        out.append(Candidate(
            code=code, text=e.title, rows=[x.row for x in entries], score=round(score, 3),
            common=sorted(t for t in common if not M.is_minor(t)),
            line_only=sorted(t for t in line_tokens - etoks - ctx if not M.is_minor(t)),
            erp_only=sorted(t for t in etoks - line_tokens if not M.is_minor(t)),
            conflict=code in idx.conflicts,
            misplaced=any(x.row in idx.misplaced_rows for x in entries),
        ))
    out.sort(key=lambda c: (-c.score, c.conflict, c.misplaced, c.code.endswith(("_", "-")), M.split_variant(c.code)[1] is not None, len(c.code), c.rows[0]))
    return out[:limit]


def _usable(c: Candidate) -> bool:
    return c.score >= EXACT_MIN and any(M.weight(t) == 3 for t in c.common)


# ── New code suggestions ───────────────────────────────────────────────────────────────────────


def _next_free(prefix: str, width: int, taken: set[str], start: int = 1) -> str:
    n = start
    while f"{prefix}{n:0{width}d}" in taken:
        n += 1
    return f"{prefix}{n:0{width}d}"


def suggest_new(line_text: str, line_tokens: set[str], near: Candidate | None, taken: set[str]) -> Suggestion:
    if line_tokens & WATERPROOFING_WORDS:
        # Follow the nearest code's numbered series (WP.01 … WP.11 → WP.12, W.P.01 … W.P.09 → W.P.10).
        base = M.split_variant(near.code)[0] if near else "WP.01"
        m = re.match(r"^(.*?)(\d+)$", base)
        if not m:
            m = re.match(r"^(.*?)(\d+)$", "WP.01")
        prefix, width = m.group(1), len(m.group(2))
        nums = [int(x.group(1)) for c in taken if (x := re.match(rf"^{re.escape(prefix)}(\d+)(?:[._][A-Z])?$", c))]
        code = _next_free(prefix, width, taken, (max(nums) + 1) if nums else 1)
        return Suggestion(code=code, why=f"Next free number in the {prefix}NN series. Not used in the ERP list yet.")
    words = [w for w in M.normalise(line_text).split() if w not in M.STOP]
    canon = [M.TERMS.get(w, w) for w in words]
    # The material wins over where it goes: "tile adhesive" is adhesive, so GROUP_PREFIX order decides.
    pre = next((p for w, p in GROUP_PREFIX.items() if w in canon), None)
    if pre is None:
        first = next((w for w in canon if M.weight(w) == 3 and w.isalpha()), "NEW")
        pre = first[:3]
    code = _next_free(f"{pre}.", 2, taken)
    return Suggestion(
        code=code,
        why=f"This ERP list only has waterproofing codes, so this needs a new group. {code} is not used yet. Ask the ERP team which group it belongs to.",
    )


def suggest_variant(code: str, taken: set[str]) -> Suggestion:
    base = M.split_variant(code)[0]
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        c = f"{base}.{letter}"
        if c not in taken:
            return Suggestion(code=c, why=f"A variant of {base} for the wider scope, if it needs its own rate. {c} is not used yet.")
    return Suggestion(code=f"{base}.NEW", why="All letter variants are taken.")


# ── Deterministic match ────────────────────────────────────────────────────────────────────────


def _words(toks) -> str:
    toks = [M.friendly(t) for t in toks]
    return ", ".join(toks[:-1]) + " and " + toks[-1] if len(toks) > 1 else (toks[0] if toks else "")


def _code_flags(code: str, text_norm: str | None, idx: _Index) -> list[Flag]:
    flags = []
    if code in idx.conflicts:
        g = idx.conflicts[code]
        flags.append(Flag(tone="bad", text=f"{code} has {len(g.members)} meanings in the ERP list: " + " / ".join(f"“{m.text}”" for m in g.members) + ". Confirm which one before using it."))
    same = sorted({c for (c, n) in idx.nodes if n == text_norm and c != code}) if text_norm else []
    near = sorted({c for g in idx.report.groups if g.kind == "near_duplicate" and code in g.codes for c in g.codes} - {code} - set(same))
    if same:
        flags.append(Flag(tone=None, text=f"Same text also under {', '.join(same)}."))
    if near:
        flags.append(Flag(tone="warn", text=f"Likely duplicate{'s' if len(near) > 1 else ''}: {', '.join(near)} {'have' if len(near) > 1 else 'has'} near-identical text."))
    for m in idx.report.misplaced:
        if m.code == code and m.text and M.normalise(m.text) == text_norm:
            flags.append(Flag(tone="warn", text=f"In Sl {m.sl_no} this code reads like {', '.join(m.reads_like)}, not like {m.base_code}. The rows may be shifted."))
    return flags


def _explain(kind: str, c: Candidate | None, line_has_wp: bool, also: list[str]) -> str:
    if kind == "none":
        if c is None:
            return "No ERP item shares any words with this line."
        return f"No ERP item is close. The nearest, {c.code} “{c.text}”, only shares {_words(c.common) or 'common words'}."
    assert c is not None
    if kind == "exact":
        return f"Same work as {c.code} “{c.text}”."
    bits = []
    if not line_has_wp:
        bits.append(f"Only {_words(c.common)} {'match' if len(c.common) > 1 else 'matches'}. {c.code} is in the waterproofing list, so it likely covers this only for waterproofing work. Other work may need its own code.")
    if c.line_only:
        bits.append(f"The request also covers {_words(c.line_only)}, which the ERP text doesn't mention. Widen the ERP text or add a variant.")
    if also:
        part = next((t for t in c.erp_only if t in ("FLOOR", "WALL")), None)
        other = {"FLOOR": "wall", "WALL": "floor"}.get(part or "", "the other part")
        bits.append(f"The request doesn't say floor or wall. The ERP splits this into {c.code} ({(part or '').lower()}) and {', '.join(also)} ({other}). Use both.")
    elif c.erp_only:
        bits.append(f"The ERP item is narrower: it is only for {_words(c.erp_only)}.")
    return " ".join(bits) or f"Close to {c.code} “{c.text}”, but not the same wording."


def match_line(line: RequirementLine, master: M.Master) -> LineMatch:
    idx = _index(master)
    toks = M.tokens(line.text)
    cands = shortlist(toks, idx)
    line_has_wp = bool(toks & WATERPROOFING_WORDS)
    flags: list[Flag] = []

    chosen = cands[0] if cands and _usable(cands[0]) else None

    # The site sheet's own code: use it if it fits about as well; flag it if it means something else.
    if line.site_code:
        sc = line.site_code.strip()
        if sc not in idx.codes:
            flags.append(Flag(tone="warn", text=f"The site sheet code {sc} isn't in the ERP list."))
        else:
            site = [c for c in shortlist(toks, idx, limit=len(idx.nodes)) if c.code == sc]
            best_site = site[0] if site else None
            if best_site and _usable(best_site) and (chosen is None or best_site.score >= chosen.score - SITE_SLACK):
                if chosen and chosen.code != sc:
                    cands = [best_site] + [c for c in cands if c.code != sc][:4]
                chosen = best_site
            elif chosen is None or chosen.code != sc:
                erp = " / ".join(f"“{es[0].title}”" for (c, _), es in idx.nodes.items() if c == sc)
                flags.append(Flag(tone="bad", text=f"The site sheet calls this {sc}, but in the ERP {sc} means {erp}. Don't use {sc} for this line."))

    also: list[str] = []
    if chosen is None:
        kind = "none"
    elif not chosen.line_only and not chosen.erp_only and line_has_wp:
        kind = "exact"
    else:
        kind = "partial"
        # "Toilets" in an email = toilet floor + toilet wall in the ERP.
        part = {"FLOOR", "WALL"} & set(chosen.erp_only)
        if len(part) == 1 and not (set(chosen.erp_only) - {"FLOOR", "WALL"}):
            other = ({"FLOOR", "WALL"} - part).pop()
            for c in shortlist(toks, idx, limit=len(idx.nodes)):
                if c.code != chosen.code and _usable(c) and other in c.erp_only and set(c.line_only) == set(chosen.line_only) and not c.conflict and not c.misplaced:
                    also = [c.code]
                    break

    suggestion = None
    if kind == "none":
        suggestion = suggest_new(line.text, toks, cands[0] if cands else None, idx.codes)
    elif kind == "partial" and not line_has_wp:
        suggestion = suggest_new(line.text, toks, chosen, idx.codes)
    elif kind == "partial" and chosen and chosen.line_only:
        suggestion = suggest_variant(chosen.code, idx.codes)

    if chosen:
        flags += _code_flags(chosen.code, M.normalise(chosen.text), idx)
        if line.site_code and line.site_code.strip() == chosen.code:
            flags.insert(0, Flag(tone="good", text=f"Matches the site sheet code {chosen.code}."))

    return LineMatch(
        line=line, kind=kind, code=chosen.code if chosen else None, erp_text=chosen.text if chosen else None,
        erp_rows=chosen.rows if chosen else [], also=also,
        reason=_explain(kind, chosen or (cands[0] if cands else None), line_has_wp, also),
        suggestion=suggestion, flags=flags, candidates=cands,
    )


def match_rules(lines: list[RequirementLine], master: M.Master) -> list[LineMatch]:
    return [match_line(ln, master) for ln in lines]


# ── Claude chooses among the shortlist (optional) ──────────────────────────────────────────────

SYSTEM = """You help the procurement back office of a residential developer in Kolkata, India, map requirement lines to ERP item codes for waterproofing work.

For each line you get a shortlist of ERP items (code and text) that a program found by shared words, plus the program's own pick. Choose the best code for the line from that shortlist only, or null if none of them is the same kind of work.

- kind "exact": the ERP item is the same work as the line.
- kind "partial": the ERP item overlaps but is wider or narrower (for example the line says "toilets" and the ERP item is only the toilet floor, or the line adds a sky terrace the ERP text doesn't mention).
- kind "none": no shortlisted item is this work. code must be null.

Write `reason` in one or two short, plain sentences a site engineer would understand. Say what matches and what doesn't. No jargon, no scores. A person makes the final decision."""


class Pick(BaseModel):
    line_id: str
    code: str | None = Field(description="A code from this line's shortlist, or null.")
    kind: Literal["exact", "partial", "none"]
    reason: str


class Picks(BaseModel):
    picks: list[Pick]


def llm_enabled() -> bool:
    return os.environ.get("ITEMCODES_LLM") != "0" and extract.has_credentials()


def _prompt(rule: list[LineMatch]) -> str:
    items = []
    for m in rule:
        items.append({
            "line_id": m.line.id,
            "line": m.line.text,
            "quantity": f"{m.line.quantity} {m.line.uom or ''}".strip() if m.line.quantity is not None else None,
            "site_sheet_code": m.line.site_code,
            "shortlist": [{"code": c.code, "erp_text": c.text} for c in m.candidates],
            "program_pick": {"code": m.code, "kind": m.kind},
        })
    return "<lines>\n" + json.dumps(items, indent=1) + "\n</lines>\n\nPick a code for every line."


def apply_picks(rule: list[LineMatch], picks: list[Pick], master: M.Master) -> list[LineMatch]:
    """Use Claude's pick where it is valid; keep the rule-based answer where it isn't."""
    idx = _index(master)
    by_id = {p.line_id: p for p in picks}
    out = []
    for m in rule:
        p = by_id.get(m.line.id)
        short = {c.code: c for c in m.candidates}
        if p is None:
            out.append(m.model_copy(update={"flags": m.flags + [Flag(tone="warn", text="Claude gave no answer for this line, so the rule-based answer is shown.")]}))
            continue
        valid = (p.code is None) == (p.kind == "none") and (p.code is None or p.code in short)
        if not valid:
            out.append(m.model_copy(update={"flags": m.flags + [Flag(tone="warn", text=f"Claude picked {p.code or 'nothing'}, which isn't on the shortlist, so the rule-based answer is shown.")]}))
            continue
        c = short.get(p.code) if p.code else None
        toks = M.tokens(m.line.text)
        if c is None:
            suggestion = suggest_new(m.line.text, toks, m.candidates[0] if m.candidates else None, idx.codes)
        elif p.code == m.code:
            suggestion = m.suggestion
        else:
            suggestion = suggest_variant(c.code, idx.codes) if p.kind == "partial" else None
        flags = [f for f in m.flags if not f.text.startswith(("Same text also", "Likely duplicates", f"{m.code} has", "In Sl"))] if p.code != m.code else m.flags
        if c and p.code != m.code:
            flags = flags + _code_flags(c.code, M.normalise(c.text), idx)
        out.append(m.model_copy(update={
            "kind": p.kind, "code": p.code, "erp_text": c.text if c else None, "erp_rows": c.rows if c else [],
            "also": m.also if p.code == m.code else [], "reason": p.reason.strip(), "suggestion": suggestion,
            "flags": flags, "decided_by": "claude",
        }))
    return out


def pick_with_claude(rule: list[LineMatch], client=None) -> list[Pick]:
    import anthropic

    client = client or extract._client()
    try:
        response = client.beta.messages.parse(
            model=extract.MODEL,
            max_tokens=8000,
            system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": _prompt(rule)}],
            output_format=Picks,
        )
    except anthropic.APIError as e:
        raise extract.IntakeError(f"Claude couldn't be reached ({type(e).__name__}).") from e
    if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
        raise extract.IntakeError("Claude's answer didn't come back complete.")
    return response.parsed_output.picks


_llm_cache: dict[str, list[Pick]] = {}


def match_lines(lines: list[RequirementLine], master: M.Master, *, use_llm: bool | None = None, client=None) -> MatchResult:
    rule = match_rules(lines, master)
    if use_llm is None:
        use_llm = llm_enabled()
    if not use_llm or not lines:
        return MatchResult(mode="rules", model=None, message="Matched by rules (shared words and a synonym list). Set ANTHROPIC_API_KEY to let Claude choose among the shortlist.", lines=rule)
    key = json.dumps([ln.model_dump() for ln in lines], sort_keys=True) + master.path
    try:
        picks = _llm_cache.get(key) if client is None else None
        if picks is None:
            picks = pick_with_claude(rule, client=client)
            if client is None:
                _llm_cache[key] = picks
    except extract.IntakeError as e:
        return MatchResult(mode="rules", model=None, message=f"{e} Showing the rule-based match.", lines=rule)
    return MatchResult(mode="claude", model=extract.MODEL, message="Claude chose among the rule-based shortlist and wrote the reasons. It can only pick listed codes.", lines=apply_picks(rule, picks, master))


# ── Lines from a demo inbox record ─────────────────────────────────────────────────────────────


def lines_from_record(record: dict, master: M.Master | None = None) -> list[RequirementLine]:
    """The quantity sheet's lines if there is one, otherwise the work type plus each scope area."""
    uid = str(record.get("uid", "x"))
    res = record.get("result", record)
    q = res.get("quantities")
    if q and q.get("lines"):
        out = []
        for ln in q["lines"]:
            qty, uom = ln.get("total_sqm"), ln.get("uom")
            if qty is None and ln.get("total_sqft") is not None:
                qty, uom = ln["total_sqft"], "SQFT"
            out.append(RequirementLine(
                id=f"{uid}-r{ln['row']}", text=ln["description"], quantity=qty, uom=uom, site_code=ln.get("code"),
                source=f"Quantity sheet row {ln['row']}", detail=ln.get("items_included"),
            ))
        return out

    req = res.get("requirement") or {}
    work = ((req.get("work_type") or {}).get("value") or "").strip()
    scopes = [s for s in ((req.get("scope_areas") or {}).get("values") or []) if s and s.strip()]
    if not work and not scopes:
        subject = ((res.get("email") or {}).get("subject") or "").strip()
        return [RequirementLine(id=f"{uid}-1", text=subject, source="Email subject")] if subject else []
    vocab = _index(master).vocab if master else set()
    # A scope area that names an ERP item ("toilets", "balconies") gets its own line. Others
    # ("north face", "12 mm: 9 MT") stay with the work type on one line.
    own = [s for s in scopes if M.tokens(s) & vocab]
    rest = [s for s in scopes if s not in own]
    out = []
    if rest or not own:
        text = work + (f" ({', '.join(rest)})" if rest else "") if work else ", ".join(rest)
        out.append(RequirementLine(id=f"{uid}-1", text=text, source="Email: work type and scope"))
    for i, s in enumerate(own, start=len(out) + 1):
        out.append(RequirementLine(id=f"{uid}-{i}", text=f"{work}: {s}" if work else s, source="Email: work type and one scope area"))
    return out
