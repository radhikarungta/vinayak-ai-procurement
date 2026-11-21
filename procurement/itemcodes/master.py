"""Load the ERP item master (.xlsx) and report its duplicate codes. No LLM.

The real master is messy: the same code appears twice with the same text, the same code appears
with two different texts, and different codes carry near-identical text. Every entry keeps the
sheet row it came from so a person can check it in the file.

Text is normalised before any comparison: case, punctuation, dotted abbreviations (W.C. -> WC),
typos seen in the sheet (HIGHT, EXPENSION, SEALENT, WATERPROOFIN), a trailing -A / -B / -1 suffix,
and a small construction synonym map (see TERMS).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = ROOT / "data" / "pack" / "04_item_codes" / "ERP_item_codes_waterproofing.xlsx"


class MasterMissing(RuntimeError):
    pass


def master_path() -> Path:
    p = os.environ.get("ITEM_MASTER_PATH")
    return Path(p) if p else DEFAULT_PATH


# ── Normalising text ───────────────────────────────────────────────────────────────────────────

# Multi-word phrases first, then single words. Canonical tokens are upper-case.
PHRASES = [
    (r"\bWATER\s*PROOF(?:ING)?\b", "WATERPROOFING"),
    (r"\bBRICK\s*BAT(?:\s+COBA)?\b", "COBA"),
    (r"\bLIFT\s*PITS?\b", "LIFTPIT"),
    (r"\bUNDER\s*GROUND\s+(?:WATER\s+)?(?:RESERVOIR|TANK|SUMP)S?\b", "UGR"),
    (r"\bOVER\s*HEAD\s+(?:WATER\s+)?TANKS?\b", "OWHT"),
    (r"\bSEWAGE\s+TREATMENT\s+PLANTS?\b", "STP"),
    (r"\bSKY\s+TERRACES?\b", "SKYTERRACE"),
    (r"\b(?:ROOF\s+TERRACE|TERRACE\s+ROOF)S?\b", "ROOF"),
    (r"\bWASH\s+PART\b", "WASH"),
    (r"\bWET\s+AREAS?\b", "WET"),
]

# Construction synonyms used on site sheets and emails. Keep this short and obvious.
TERMS = {
    "WC": "TOILET", "BATHROOM": "TOILET", "BATH": "TOILET", "LAVATORY": "TOILET", "TOILET": "TOILET",
    "UTILITY": "WASH",
    # F.R.P. (fire refuge platform) is an open slab that site sheets measure with the balconies.
    "FRP": "BALCONY",
    # On its own, "terrace" means the roof terrace. A sky terrace is kept apart (see PHRASES).
    "TERRACE": "ROOF", "ROOFTOP": "ROOF",
    "MUMTY": "MUMPTY", "MUMTEE": "MUMPTY", "MUMPTY": "MUMPTY",
    "SUMP": "UGR", "OHT": "OWHT", "KOBA": "COBA", "BRICKBAT": "COBA",
    "GROUT": "GROUTING",
    "SCAFFOLD": "SCAFFOLDING", "CUPLOCK": "SCAFFOLDING", "STAGING": "SCAFFOLDING",
    "HIRE": "CHARGE", "RENT": "CHARGE", "RENTAL": "CHARGE",
    "PUMP": "PUMPING", "SUNKEN": "DEPRESSION", "WATERPROOF": "WATERPROOFING",
    "TILING": "TILE",
}

# Things a construction person reads as implied: brick bat coba is laid on the roof.
IMPLIES = {"COBA": {"ROOF"}}

TYPOS = [(r"\bHIGHT\b", "HEIGHT"), (r"\bEXPENSION\b", "EXPANSION"), (r"\bSEALENT\b", "SEALANT"), (r"\bWATERPROOFIN(?!G)", "WATERPROOFING ")]

STOP = {"AT", "THE", "OF", "IN", "FOR", "AND", "WITH", "TO", "ALL", "ON", "BY", "AS", "PER", "WORK", "A", "AN", "OR", "IS", "BE", "FROM", "ITS"}

# Counted as common words (low weight): most items in a waterproofing list share them.
GENERIC = {"WATERPROOFING", "FLOOR", "WALL", "TREATMENT", "CHARGE", "INSIDE", "OUTSIDE", "WET", "CONCRETE"}

# Ignored when deciding whether two texts mean the same work.
MINOR = {
    "MM", "HEIGHT", "UPTO", "IF", "REQUIRED", "SUPPLY", "APPLICATION", "PROVIDING", "APPLYING", "FIXING",
    "INTERNAL", "EXTERNAL", "MAIN", "AREA", "PART", "TOP", "NORTH", "SOUTH", "EAST", "WEST", "FACE",
    "NOS", "NO", "SQM", "SQFT", "SFT", "RM", "BAG", "MT", "KG", "LOT", "FLAT", "TYPICAL", "TOWER", "BLOCK",
    "METHODOLOGY", "FLOORS",
}

FRIENDLY = {
    "TOILET": "toilet", "COBA": "brick bat coba", "LIFTPIT": "lift pit", "UGR": "UGR (underground tank)",
    "OWHT": "overhead tank", "STP": "STP", "SKYTERRACE": "sky terrace", "WASH": "wash area", "WET": "wet area",
    "MUMPTY": "mumty", "RCC": "RCC", "CHARGE": "charges",
}

# "-A", "-B", "-1" at the end marks a variant row. "METHODOLOGY-1" is a real difference, so it stays.
_SUFFIX = re.compile(r"(?<!METHODOLOGY)\s*-\s*[A-Z0-9]\s*$")
_DOTTED = re.compile(r"\b((?:[A-Z]\.)+[A-Z]?)(?![A-Z])")
_SPEC_START = re.compile(r"\b(PROVIDING|SUPPLYING AND|SUPPLY AND APPLYING)\b")


def title_of(text: str) -> str:
    """The name part of a description. A long spec ("... Providing and applying ...") is cut off."""
    t = re.sub(r"\s+", " ", text or "").strip()
    m = _SPEC_START.search(t.upper())
    if m and m.start() > 10 and len(t) > 150:
        t = t[: m.start()].strip(" ,;:-")
    return t


def normalise(text: str) -> str:
    """Upper-case, typos fixed, dotted abbreviations closed up, -A/-B suffix and punctuation dropped."""
    t = (text or "").upper().replace("’", "'")
    for pat, rep in TYPOS:
        t = re.sub(pat, rep, t)
    t = _SUFFIX.sub("", t.strip())
    t = _DOTTED.sub(lambda m: m.group(1).replace(".", ""), t)
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _word(w: str) -> str:
    if len(w) > 4 and w.endswith("IES"):
        w = w[:-3] + "Y"
    elif len(w) > 3 and w.endswith("S") and not w.endswith("SS"):
        w = w[:-1]
    return TERMS.get(w, w)


def tokens(text: str) -> set[str]:
    """Canonical words of a text, for matching. Stop words dropped, synonyms folded."""
    t = normalise(text)
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    out = {_word(w) for w in t.split() if w not in STOP}
    out -= STOP
    for k, extra in IMPLIES.items():
        if k in out:
            out |= extra
    return out


def is_minor(tok: str) -> bool:
    return tok in MINOR or any(c.isdigit() for c in tok)


def weight(tok: str) -> int:
    return 0 if is_minor(tok) else 1 if tok in GENERIC else 3


def friendly(tok: str) -> str:
    return FRIENDLY.get(tok, tok.lower())


# Code parts that say what the item is for (STP_UGR is grouting for the STP and UGR).
CODE_CONTEXT = {"UGR": "UGR", "STP": "STP", "COBA": "COBA", "EXP": "EXPANSION", "OWHT": "OWHT"}


def code_tokens(code: str) -> set[str]:
    return {CODE_CONTEXT[p] for p in re.split(r"[._\-\s]+", code.upper()) if p in CODE_CONTEXT}


_VARIANT = re.compile(r"^(.*?)[._]([A-Z])$")


def split_variant(code: str) -> tuple[str, str | None]:
    """WP.04.B -> (WP.04, B). W.P.11B and WP.MR are not variants."""
    m = _VARIANT.match(code)
    if m and re.search(r"\d", m.group(1)):
        return m.group(1), m.group(2)
    return code, None


# ── The master ─────────────────────────────────────────────────────────────────────────────────


class Entry(BaseModel):
    row: int  # sheet row in the .xlsx
    sl_no: str | None
    code: str
    description: str
    title: str  # description without a long spec tail
    has_spec: bool
    normalised: str
    tokens: list[str]
    base_code: str
    variant: str | None


class Master(BaseModel):
    path: str
    sheet: str
    title: str | None
    entries: list[Entry]

    def codes(self) -> set[str]:
        return {e.code for e in self.entries}

    def by_code(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = defaultdict(list)
        for e in self.entries:
            out[e.code].append(e)
        return out


def _cell(v: object) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def make_entry(row: int, sl_no: object, code: str, description: str) -> Entry:
    title = title_of(description)
    base, variant = split_variant(code)
    return Entry(
        row=row,
        sl_no=_cell(sl_no) or None,
        code=code,
        description=description,
        title=title,
        has_spec=title != re.sub(r"\s+", " ", description).strip(),
        normalised=normalise(title),
        tokens=sorted(tokens(title)),
        base_code=base,
        variant=variant,
    )


def load_master(path: Path | None = None) -> Master:
    path = Path(path) if path else master_path()
    if not path.exists():
        raise MasterMissing(
            f"The ERP item master isn't here: {path}. Put the file there or set ITEM_MASTER_PATH to its location."
        )
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        for h, row in enumerate(rows[:20]):
            cells = [_cell(c).upper() for c in row]
            ci = next((i for i, c in enumerate(cells) if "CODE" in c), None)
            di = next((i for i, c in enumerate(cells) if "DESCRIPTION" in c), None)
            if ci is None or di is None:
                continue
            si = next((i for i, c in enumerate(cells) if c.startswith("SL")), None)
            entries = []
            for r, vals in enumerate(rows[h + 1 :], start=h + 2):
                code, desc = _cell(vals[ci] if ci < len(vals) else None), _cell(vals[di] if di < len(vals) else None)
                if code and desc:
                    entries.append(make_entry(r, vals[si] if si is not None else None, code, desc))
            title = " · ".join(_cell(r[0]) for r in rows[:h] if r and r[0]) or None
            wb.close()
            return Master(path=str(path), sheet=ws.title, title=title, entries=entries)
    wb.close()
    raise MasterMissing(f"No sheet in {path.name} has an 'Item Code' and 'Item Description' header.")


_cache: dict[str, tuple[float, Master]] = {}


def get_master() -> Master:
    """Load once per file version."""
    path = master_path()
    if not path.exists():
        raise MasterMissing(
            f"The ERP item master isn't here: {path}. Put the file there or set ITEM_MASTER_PATH to its location."
        )
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    m = load_master(path)
    _cache[str(path)] = (mtime, m)
    return m


# ── Duplicate report ───────────────────────────────────────────────────────────────────────────


class Member(BaseModel):
    code: str
    text: str
    rows: list[int]
    sl_nos: list[str]


class DuplicateGroup(BaseModel):
    kind: str  # "repeated" | "same_code_different_text" | "near_duplicate"
    codes: list[str]
    members: list[Member]
    note: str


class Misplaced(BaseModel):
    row: int
    sl_no: str | None
    code: str
    text: str
    base_code: str
    base_text: str
    reads_like: list[str]


class DuplicateReport(BaseModel):
    rows: int
    codes: int
    groups: list[DuplicateGroup]
    misplaced: list[Misplaced]
    notes: list[str]

    def count(self, kind: str) -> int:
        return sum(1 for g in self.groups if g.kind == kind)


def similar(a: set[str], b: set[str]) -> bool:
    """Near-identical wording: same words, or one extra word on a text of three or more words."""
    if not a or not b:
        return False
    if a == b:
        return True
    inter = len(a & b)
    if inter / len(a | b) >= 0.8:
        return True
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    extra = big - small
    # One extra word is still the same item ("nozzle grouting" vs "grouting"), unless that word is
    # floor/wall/inside: those split one item into two.
    return small <= big and len(small) >= 3 and len(extra) == 1 and not (extra & GENERIC)


def _member(code: str, entries: list[Entry]) -> Member:
    return Member(code=code, text=entries[0].title, rows=[e.row for e in entries], sl_nos=[e.sl_no or "" for e in entries])


def _sl(e: Entry) -> str:
    return f"Sl {e.sl_no}" if e.sl_no else f"row {e.row}"


def duplicate_report(master: Master) -> DuplicateReport:
    groups: list[DuplicateGroup] = []
    by_code = master.by_code()

    # Each distinct (code, normalised text) is one node.
    nodes: dict[tuple[str, str], list[Entry]] = defaultdict(list)
    for e in master.entries:
        nodes[(e.code, e.normalised)].append(e)

    for code, entries in by_code.items():
        texts: dict[str, list[Entry]] = defaultdict(list)
        for e in entries:
            texts[e.normalised].append(e)
        for es in texts.values():
            if len(es) > 1:
                groups.append(DuplicateGroup(
                    kind="repeated", codes=[code], members=[_member(code, es)],
                    note=f"{code} is listed {len(es)} times with the same text ({', '.join(_sl(e) for e in es)}). Keep one row.",
                ))
        if len(texts) > 1:
            members = [_member(code, es) for es in texts.values()]
            groups.append(DuplicateGroup(
                kind="same_code_different_text", codes=[code], members=members,
                note=f"{code} means {len(texts)} different things: "
                + "; ".join(f"“{m.text}” ({', '.join('Sl ' + s if s else 'row ' + str(r) for s, r in zip(m.sl_nos, m.rows))})" for m in members)
                + ". A PO with this code is ambiguous. Decide which one is right.",
            ))

    # Near duplicates: different codes whose texts are near-identical. Group connected nodes.
    keys = list(nodes)
    toks = {k: set(nodes[k][0].tokens) for k in keys}
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if a[0] != b[0] and similar(toks[a], toks[b]):
                parent[find(a)] = find(b)
    comps: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for k in keys:
        comps[find(k)].append(k)
    for comp in comps.values():
        codes = sorted({k[0] for k in comp})
        if len(codes) < 2:
            continue
        comp.sort(key=lambda k: nodes[k][0].row)
        members = [_member(k[0], nodes[k]) for k in comp]
        bases = {split_variant(c)[0] for c in codes}
        if len(bases) == 1:
            note = f"{', '.join(codes)} carry the same text. The A/B codes may be two specs of one item; if not, keep one code."
        else:
            note = f"{', '.join(codes)} have near-identical text under different codes. Check whether they are the same item and keep one."
        groups.append(DuplicateGroup(kind="near_duplicate", codes=codes, members=members, note=note))

    # Variant rows whose text disagrees with their base code but matches another code's text.
    misplaced: list[Misplaced] = []
    base_text = {c: es[0] for c, es in by_code.items()}
    text_codes: dict[str, set[str]] = defaultdict(set)
    for e in master.entries:
        text_codes[e.normalised].add(e.code)
    for e in master.entries:
        if not e.variant or e.base_code not in base_text:
            continue
        b = base_text[e.base_code]
        if similar(set(e.tokens), set(b.tokens)):
            continue
        like = sorted(c for c in text_codes[e.normalised] if split_variant(c)[0] != e.base_code)
        if like:
            misplaced.append(Misplaced(row=e.row, sl_no=e.sl_no, code=e.code, text=e.title, base_code=e.base_code, base_text=b.title, reads_like=like))

    notes: list[str] = []
    run: list[Misplaced] = []
    for m in misplaced + [None]:  # type: ignore[list-item]
        if m is not None and run and m.row == run[-1].row + 1:
            run.append(m)
            continue
        if len(run) >= 3:
            notes.append(
                f"{_sl_m(run[0])} to {_sl_m(run[-1])} ({run[0].code} to {run[-1].code}) look shifted by one row: "
                f"each code there carries the text of the next code."
            )
        run = [m] if m is not None else []

    order = {"same_code_different_text": 0, "near_duplicate": 1, "repeated": 2}
    groups.sort(key=lambda g: (order[g.kind], min(min(m.rows) for m in g.members)))
    return DuplicateReport(rows=len(master.entries), codes=len(by_code), groups=groups, misplaced=misplaced, notes=notes)


def _sl_m(m: Misplaced) -> str:
    return f"Sl {m.sl_no}" if m.sl_no else f"row {m.row}"
