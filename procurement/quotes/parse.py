"""Read a quotation PDF's text layer into a Quote. No LLM.

A BOQ row looks like this in the layout text, whatever program made the PDF:

    1  WATERPROOFING AT TOILET FLOOR        SQFT   13,429.53   47.00   6,31,187.91
       (description continues on the next lines …)

Serial number, a description, a unit, then up to three figures: quantity, rate, amount (a "-" holds a
blank figure's place). With two figures, the column header says whether they are qty + rate or rate +
amount; with no header it is read as qty + rate and the quote says so. Rows that look like BOQ rows
but don't parse (an unknown unit, say) are listed in the notes, not dropped silently.
Indian digit grouping ("6,31,187.91") is fine. A scan has no usable text layer (or one without
the rates, e.g. handwritten figures), so it is marked needs_vision with low confidence. We don't
guess the missing numbers.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .models import Quote, QuoteLine, Term

# Unit as written -> normalised (same names the deals store uses: sqft, sqm, rmt).
UNITS: dict[str, str] = {
    "SQFT": "sqft", "SQ.FT": "sqft", "SQ FT": "sqft", "SFT": "sqft", "SOFT": "sqft",  # SOFT: OCR of SQFT
    "SQM": "sqm", "SQ.M": "sqm", "SQ M": "sqm", "SQMT": "sqm", "SQMTR": "sqm", "M2": "sqm",
    "RMT": "rmt", "RM": "rmt", "R.M": "rmt", "RMTR": "rmt",
    "NOS": "nos", "NO": "nos", "KG": "kg", "MT": "mt", "CUM": "cum",
}
_COMPACT = sorted({re.sub(r"[.\s]", "", k) for k in UNITS}, key=len, reverse=True)
# Every UNITS spelling, with an optional dot or space between letters (SQ.MT, R.M.T., SQ FT …).
_UNIT_RE = "(?:" + "|".join(r"\.?\s?".join(map(re.escape, k)) for k in _COMPACT) + r")\.?"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_FIG = rf"(?:{_NUM}|[-–—]+)"  # a dash holds a blank figure's place
_ROW = r"^\s*\|?\s*(?P<sl>\d{1,3})\s*[|.)]?\s+(?P<desc>[A-Za-z][^\n]*?)\s{2,}"
LINE_RE = re.compile(
    rf"{_ROW}(?P<unit>{_UNIT_RE})(?:\s+(?P<n1>{_FIG}))?(?:\s+(?P<n2>{_FIG}))?(?:\s+(?P<n3>{_FIG}))?\s*$",
    re.I,
)
# Looks like a BOQ row (serial, text, figures at the end) but LINE_RE didn't take it.
BOQ_LIKE_RE = re.compile(rf"{_ROW}(?:\S+\s+)?{_FIG}(?:\s+{_FIG})*\s*$")
# A column header: rate plus unit or quantity, laid out in columns (gaps of 2+ spaces).
HEADER_RE = re.compile(r"^(?=.*\brate\b)(?=.*\b(?:unit|uom|qty|[qo]uantity)\b)(?=(?:.*\S\s{2,}\S){2})", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b")


def norm_unit(u: str) -> str:
    key = re.sub(r"\s+", " ", u.strip().upper()).rstrip(".")
    return UNITS.get(key) or UNITS.get(key.replace(" ", "")) or UNITS.get(re.sub(r"[.\s]", "", key), key.lower())


def num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def pdf_pages(path: Path) -> tuple[list[str], str]:
    """Layout text of each page, and the PDF's producer/creator string."""
    from pypdf import PdfReader

    r = PdfReader(str(path))
    meta = r.metadata or {}
    producer = " / ".join(str(x) for x in (meta.get("/Producer"), meta.get("/Creator")) if x)
    pages = []
    for p in r.pages:
        try:
            pages.append(p.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001 - a broken page shouldn't stop the rest
            pages.append(p.extract_text() or "")
    return pages, producer


def _columns(header: str) -> list[str]:
    """The figure columns a header line names, in order: some of qty, rate, amount."""
    low = header.lower()
    cols = []
    if re.search(r"\b[qo]uantity\b|\bqn?ty\b", low):  # OUANTITY: OCR of QUANTITY
        cols.append("qty")
    cols.append("rate")
    if re.search(r"\bamount\b|\bamt\b|\bvalue\b|\btotal\b", low):
        cols.append("amount")
    return cols


def parse_lines(pages: list[str], notes: list[str] | None = None) -> list[QuoteLine]:
    """BOQ rows from layout text. The first row seen for a serial number wins (page headers repeat).
    What couldn't be read for sure goes in `notes`: rows that didn't parse, two figures with no header."""
    out: dict[int, QuoteLine] = {}
    cols: list[str] | None = None  # from the last column header seen
    skipped: list[str] = []
    guessed: list[int] = []
    derived: list[int] = []
    for pi, text in enumerate(pages, 1):
        for raw in text.splitlines():
            m = LINE_RE.match(raw)
            if not m:
                if BOQ_LIKE_RE.match(raw):
                    skipped.append(f"page {pi}: “{' '.join(raw.split())}”")
                elif HEADER_RE.search(raw) and not re.match(r"^\s*\|?\s*\d", raw):
                    cols = _columns(raw)
                continue
            sl = int(m["sl"])
            if sl in out or sl == 0:
                continue
            figs = [m[k] for k in ("n1", "n2", "n3") if m[k]]
            nums = [num(x) for x in figs]
            # The header only decides two figures (qty + rate or rate + amount); otherwise they go in order.
            got = dict(zip(cols, nums)) if cols and len(nums) == 2 == len(cols) else dict(zip(("qty", "rate", "amount"), nums))
            qty, rate, amount = got.get("qty"), got.get("rate"), got.get("amount")
            if len(nums) == 2 and len(cols or []) < 2:
                guessed.append(sl)
            if qty is None and "qty" not in got and rate and amount is not None:
                qty = round(amount / rate, 2)
                derived.append(sl)
            line = QuoteLine(sl_no=sl, description=re.sub(r"\s+", " ", m["desc"]).strip(" |"), unit=norm_unit(m["unit"]),
                             unit_as_written=m["unit"].strip(), qty=qty, rate=rate, amount=amount, page=pi)
            if qty is not None and rate is not None and amount is not None and sl not in derived:
                line.arithmetic_ok = abs(qty * rate - amount) <= max(1.0, 0.001 * amount)
            out[sl] = line
    if notes is not None:
        if skipped:
            notes.append(f"{len(skipped)} row{'s' * (len(skipped) != 1)} that look like BOQ rows couldn't be read (unit or layout not "
                         f"recognised), so they are not in the comparison: {'; '.join(skipped[:3])}{' …' if len(skipped) > 3 else ''}")
        if guessed:
            notes.append(f"Two figures and no column header on line {', '.join(map(str, guessed))}: read as quantity and rate. Check the page.")
        if derived:
            notes.append(f"No quantity column: quantity worked out as amount / rate on line {', '.join(map(str, derived))}.")
    return [out[k] for k in sorted(out)]


def find_date(pages: list[str]) -> str | None:
    """First dd.mm.yyyy on the first page that has one (quotes put the date in the header)."""
    for text in pages[:2]:
        for m in DATE_RE.finditer(text):
            d, mo, y = (int(x) for x in m.groups())
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def find_terms(pages: list[str]) -> list[Term]:
    out: list[Term] = []
    seen: set[str] = set()

    def add(key: str, value: str, page: int, text: str) -> None:
        if key not in seen:
            seen.add(key)
            out.append(Term(key=key, value=value, page=page, text=text.strip()))

    for pi, text in enumerate(pages, 1):
        for raw in text.splitlines():
            ln = re.sub(r"\s+", " ", raw).strip()
            low = ln.lower()
            if LINE_RE.match(raw):
                continue
            if m := re.search(r"g\.?s\.?t\.?\s*@?\s*(\d{1,2})\s*%", low):
                add("gst", f"{m[1]}% extra", pi, ln)
            elif re.search(r"(excluding|extra).{0,20}g\.?s\.?t|g\.?s\.?t.{0,30}extra", low):
                add("gst", "extra, rate not stated", pi, ln)
            if m := re.search(r"validity.{0,40}?(\d{1,3})\s*days", low):
                add("validity_days", m[1], pi, ln)
            if m := re.search(r"(\d{1,3})\s*%\s*(?:payment|advance|material secured advance)", low):
                add("payment", ln, pi, ln)
            elif re.search(r"payment terms?\b|terms of payment|\b(r/?a|running)\b.{0,15}bill.{0,60}\d+\s*\(?\w*\)?\s*days", low):
                add("payment", ln, pi, ln)
            if m := re.search(r"(?:start|commence|mobili[sz]e)\w*.{0,40}within\s*(\d{1,3})\s*days", low):
                add("lead_time_days", m[1], pi, ln)
            if m := re.search(r"warranty.{0,30}?(\d{1,2})\s*years?", low):
                add("warranty", f"{m[1]} years", pi, ln)
    return out


def vendor_from_filename(path: Path) -> str:
    return path.stem.removeprefix("Quotation_").removeprefix("Quote_").replace("_", " ").strip()


def build_quote(vendor: str, file: str, pages: list[str], producer: str = "") -> Quote:
    notes: list[str] = []
    lines = parse_lines(pages, notes)
    unsure = bool(notes)  # rows not read, or figures read by position only
    priced = [ln for ln in lines if ln.rate is not None]
    scan = "scan" in producer.lower()
    needs_vision = False
    if not lines:
        needs_vision = True
        notes.append("No BOQ rows found in the text layer. Read it by eye or with a vision model.")
    elif len(priced) < len(lines):
        needs_vision = True
        if scan:
            notes.append(f"A scan: the text layer gives {len(lines)} BOQ row{'s' * (len(lines) != 1)}, {len(priced)} with a rate "
                         "(the figures are probably handwritten). Not guessed: read it by eye or with a vision model.")
        else:
            notes.append(f"{len(lines) - len(priced)} of {len(lines)} rows have no rate in the text layer. "
                         "Not guessed: read it by eye or with a vision model.")
    if needs_vision:
        confidence = "low"
    else:
        checks = [ln.arithmetic_ok for ln in lines if ln.arithmetic_ok is not None]
        if checks and all(checks) and len(checks) == len(lines) and not unsure:
            confidence = "high"
        else:
            confidence = "medium"
            if not checks and all(ln.amount is None for ln in lines):
                notes.append("No amount column, so qty x rate couldn't be cross-checked.")
            elif not all(checks):
                bad = [str(ln.sl_no) for ln in lines if ln.arithmetic_ok is False]
                notes.append(f"Amount isn't qty x rate on line {', '.join(bad)}.")
    if scan and not needs_vision:
        notes.append("Made by a scanning app: check the figures against the page.")
    return Quote(vendor=vendor, file=file, producer=producer, date=find_date(pages), lines=lines, terms=find_terms(pages),
                 read_confidence=confidence, needs_vision=needs_vision, notes=notes)


def read_quote(path: Path) -> Quote:
    pages, producer = pdf_pages(path)
    return build_quote(vendor_from_filename(path), path.name, pages, producer)


def unreadable(path: Path, err: Exception) -> Quote:
    """A PDF that couldn't be opened (encrypted, damaged): listed as needing a person, not a 500."""
    return Quote(vendor=vendor_from_filename(path), file=path.name, lines=[], read_confidence="low", needs_vision=True,
                 notes=[f"The PDF couldn't be read ({type(err).__name__}: {err}). Open it by hand or ask the vendor to resend it."])


def read_quote_safe(path: Path) -> Quote:
    try:
        return read_quote(path)
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the other quotes
        return unreadable(path, e)
