"""Seed the vendor directory from the data pack.

    uv run python -m procurement.vendors.seed

Sources (in data/pack, git-ignored; override the folder with VENDORS_PACK):
- 05_vendors/Contractor_list_<Project>.xlsx: one column "Contractor", names only, for one project.
- 06_quotations/Quotation_<Vendor>.pdf: the vendors who quoted for the waterproofing tender.
  The PDFs aren't parsed here; the names come from the file names.
No real vendor or project names live in this code: the repo is public, the pack is not.

Safe to run again: vendors are matched by normalised name, and only missing names, sources,
projects and categories are added. Nothing a person typed is overwritten.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from . import store

DEFAULT_PACK = store.ROOT / "data" / "pack"
LIST_SOURCE = "Project contractor list"
QUOTE_SOURCE = "Quoted for the waterproofing tender"
QUOTE_PROJECT = "Tower-2 waterproofing tender"

# Obvious categories from words in a name. These are guesses, so they are saved as "to confirm".
INFER: list[tuple[str, str]] = [
    (r"\belectricals?\b", "Electrical"),
    (r"\bfurniture\b", "Furniture"),
    (r"\b(testing|laborator(y|ies)|lab)\b", "Testing & lab"),
    (r"\bpest control\b", "Pest control"),
    (r"\belevators?\b|\blifts?\b", "Lifts"),
    (r"\bfire\b", "Fire safety"),
    (r"\bsolar\b", "Solar"),
    (r"\bglazing\b", "Glazing"),
    (r"\balumin(i)?um\b", "Aluminium work"),
    (r"\bpiling\b", "Piling"),
    (r"\bsurvey(ors?)?\b", "Survey"),
    (r"\bconcrete\b", "Concrete"),
    (r"\bfabricators?\b", "Fabrication"),
    (r"\bcooling\b", "Cooling & HVAC"),
    (r"\btele(kom|com)\b", "Telecom"),
    (r"\birrigation\b", "Irrigation"),
    (r"\binterio(r|rs)?\b", "Interiors"),
    (r"\babrasives?\b", "Tools & consumables"),
    (r"\bwaterproof(ing)?\b", "Waterproofing"),
]


def pack_dir() -> Path:
    return Path(os.environ.get("VENDORS_PACK") or DEFAULT_PACK)


def infer_categories(name: str) -> list[str]:
    low = name.lower()
    return [cat for pat, cat in INFER if re.search(pat, low)]


def read_contractor_list(path: Path) -> list[str]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(c or "").strip().lower() for c in next(rows, [])]
    col = header.index("contractor") if "contractor" in header else 0
    names = []
    for row in rows:
        if row and col < len(row) and row[col] and str(row[col]).strip():
            names.append(re.sub(r"\s+", " ", str(row[col])).strip())
    wb.close()
    return names


def quote_vendors(folder: Path) -> list[str]:
    out = []
    for pdf in sorted(folder.glob("Quotation_*.pdf")):
        stem = pdf.stem.removeprefix("Quotation_")
        out.append(stem.replace("_", " "))
    return out


def match(vendors: list[store.Vendor], name: str) -> store.Vendor | None:
    """Same normalised name; otherwise a unique vendor whose name starts with all of this name's words
    ("Acme" → "ACME SALES & SERVICES PRIVATE LIMITED"), or the other way round."""
    if (v := store.find_by_name(vendors, name)) is not None:
        return v
    t = store.name_tokens(name)
    if not t:
        return None
    hits = []
    for v in vendors:
        vt = store.name_tokens(v.name)
        short, long_ = (t, vt) if len(t) <= len(vt) else (vt, t)
        if short and long_[: len(short)] == short and len(" ".join(short)) >= 5:
            hits.append(v)
    return hits[0] if len(hits) == 1 else None


def _add_unique(lst: list[str], items: list[str]) -> bool:
    changed = False
    for i in items:
        if i.lower() not in {x.lower() for x in lst}:
            lst.append(i)
            changed = True
    return changed


def run(quiet: bool = False) -> dict:
    """Seed or top up the store. Returns counts and a plain message."""
    say = (lambda *_: None) if quiet else print
    pack = pack_dir()
    lists = sorted((pack / "05_vendors").glob("Contractor_list_*.xlsx")) if (pack / "05_vendors").is_dir() else []
    quotes = pack / "06_quotations"
    entries: list[tuple[str, dict]] = []  # (name, {source, project, categories, inferred})
    missing = []

    for lst in lists:
        project = lst.stem.removeprefix("Contractor_list_").replace("_", " ")
        for n in read_contractor_list(lst):
            inf = infer_categories(n)
            entries.append((n, {"source": LIST_SOURCE, "project": project, "categories": inf, "inferred": inf}))
    if not lists:
        missing.append(str(pack / "05_vendors" / "Contractor_list_*.xlsx"))
    if quotes.is_dir() and any(quotes.glob("Quotation_*.pdf")):
        for n in quote_vendors(quotes):
            entries.append((n, {"source": QUOTE_SOURCE, "project": QUOTE_PROJECT, "categories": ["Waterproofing"], "inferred": []}))
    else:
        missing.append(str(quotes / "Quotation_*.pdf"))

    path = store.store_path()
    vendors = store.load_all() if path.exists() else []
    created = merged = updated = 0
    with store._lock:
        t = store.now()
        new_ids: set[str] = set()
        for name, e in entries:
            v = match(vendors, name)
            if v is None:
                v = store.Vendor(id=uuid.uuid4().hex[:12], name=name, created_at=t, updated_at=t)
                vendors.append(v)
                new_ids.add(v.id)
                created += 1
            elif v.id in new_ids and e["source"] not in v.sources:
                merged += 1  # two sources in this run point to the same vendor
            changed = _add_unique(v.sources, [e["source"]])
            changed |= _add_unique(v.projects, [e["project"]])
            # A confirmed category stays confirmed; a new guess is added as "to confirm".
            new_cats = [c for c in e["categories"] if c.lower() not in {x.lower() for x in v.categories}]
            if new_cats:
                v.categories.extend(new_cats)
                v.categories_to_confirm.extend(c for c in new_cats if c in e["inferred"])
                changed = True
            if changed and v.id not in new_ids:
                v.updated_at = t
                updated += 1
        if entries or not path.exists():
            store.save_all(vendors)

    both = sum(1 for v in vendors if LIST_SOURCE in v.sources and QUOTE_SOURCE in v.sources)
    if not entries:
        msg = f"No data pack found ({', '.join(missing)}). The directory starts empty; add vendors by hand or put the pack in {pack} and run this again."
    else:
        msg = f"{len(vendors)} vendors in {path}: {created} new, {updated} existing ones topped up, {both} found in both the contractor list and the quotes."
        if missing:
            msg += f" Not found, skipped: {', '.join(missing)}."
    say(msg)
    return {"vendors": len(vendors), "created": created, "updated": updated, "merged_this_run": merged,
            "in_both_sources": both, "missing": missing, "message": msg}


if __name__ == "__main__":
    run()
