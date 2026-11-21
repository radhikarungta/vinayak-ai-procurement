"""Read-only tools for the chat assistant.

Each tool calls the feature code in-process (the same functions the HTTP endpoints use) and returns
compact JSON. Nothing here writes. Errors come back as {"error": ...} with is_error set, so the model
can say plainly that the data isn't there.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from fastapi import HTTPException

MAX_CHARS = 24000  # per tool result
MAX_ROWS = 40


class ToolError(RuntimeError):
    pass


# ── Helpers ────────────────────────────────────────────────────────────────────────────────────


def _cap(items: list, n: int = MAX_ROWS) -> dict:
    """A list, truncated, with the total so the model knows there is more."""
    out: dict[str, Any] = {"total": len(items), "items": items[:n]}
    if len(items) > n:
        out["truncated"] = f"Showing {n} of {len(items)}. Ask a narrower question to see the rest."
    return out


def _call(fn: Callable, *args, **kw):
    """Call an endpoint function; turn its HTTP errors into a tool error."""
    try:
        return fn(*args, **kw)
    except HTTPException as e:
        raise ToolError(str(e.detail)) from e


def _short(s: Any, n: int = 160) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[: n - 1] + "…"
    return s


def _inr(x: float | None) -> str | None:
    from ..quotes.compare import inr

    return None if x is None else inr(x)


def _lakh(x: float | None) -> str | None:
    return None if x is None else f"₹{x / 100000:,.2f} lakh"


def _uid(uid: str) -> str:
    uid = str(uid or "").strip().lstrip("#")
    uid = re.sub(r"^(case|requirement|req)\s*", "", uid, flags=re.I)
    if not uid.isalnum():
        raise ToolError("A requirement id is letters and digits only, e.g. 1010 or m42.")
    return uid


# ── Requirements (cases) ───────────────────────────────────────────────────────────────────────

STAGES = ["item_codes", "waiting_on_site", "approve", "vendors", "rfq", "rfq_sent", "quotes", "hod", "done", "error"]  # flow.Stage + "error"


def _dates(uid: str) -> dict:
    """When replies and RFQs went out, from the saved decisions."""
    from ..cases import flow as F

    try:
        st = F.load_state(uid)
    except Exception:
        return {}
    replies = [r.get("at") for r in st.get("replies", []) if r.get("at")]
    rfqs = [{"vendor": r.get("vendor"), "category": r.get("category"), "at": r.get("at"), "via": r.get("via")} for r in st.get("rfqs", [])]
    return {"last_reply_to_site_at": replies[-1] if replies else None, "replies_to_site": len(replies),
            "approved_at": st.get("approved_at"), "rfqs_sent": rfqs}


def list_requirements(stage: str | None = None) -> dict:
    from ..cases import api as cases_api

    data = _call(cases_api.list_cases)
    if not data.get("ready"):
        raise ToolError(data.get("message") or "The requirement list isn't available.")
    items = data["items"]
    if stage:
        items = [i for i in items if i.get("stage") == stage]
    out = []
    for i in items:
        row = {k: v for k, v in i.items() if k not in ("counts",)}
        row["open_counts"] = {k: v for k, v in (i.get("counts") or {}).items() if v}
        row |= _dates(str(i["uid"]))
        out.append(row)
    return {"stage_filter": stage, "stages": STAGES} | _cap(out)


def _line(ln: dict) -> dict:
    m = ln.get("match") or {}
    out = {
        "id": ln.get("id"), "text": ln.get("text"), "quantity": ln.get("quantity"), "uom": ln.get("uom"),
        "source": ln.get("source"), "status": ln.get("status"), "codes": ln.get("codes"), "category": ln.get("category"),
        "issues": [f"{i.get('kind')}: {i.get('text')}" for i in ln.get("issues") or []],
        "match": {"kind": m.get("kind"), "code": m.get("code"), "erp_text": m.get("erp_text"), "also": m.get("also"),
                  "reason": m.get("reason"), "flags": [f.get("text") for f in m.get("flags") or []],
                  "candidates": [{"code": c.get("code"), "text": c.get("text")} for c in (m.get("candidates") or [])[:3]]},
    }
    if ln.get("decision"):
        out["decision"] = ln["decision"]
    return out


def get_requirement(uid: str) -> dict:
    from ..cases import api as cases_api

    full = _call(cases_api.get_case, _uid(uid))
    skip = {"lines", "email", "requirement", "draft", "rfq_drafts", "send"}
    out = {k: v for k, v in full.items() if k not in skip}
    out["lines"] = [_line(ln) for ln in full.get("lines", [])]
    for r in out.get("replies") or []:
        r["body"] = _short(r.get("body"), 400)
    for r in out.get("rfqs") or []:
        r["body"] = _short(r.get("body"), 300)
    out["rfq_drafts_ready"] = len(full.get("rfq_drafts") or [])
    out["note"] = "Use read_email for the original email text and the extracted requirement."
    return out


def read_email(uid: str) -> dict:
    from ..cases import flow as F

    try:
        rec = F.record(_uid(uid))
    except F.CaseError as e:
        raise ToolError(str(e)) from e
    res = rec.get("result") or {}
    email = res.get("email") or {}
    q = res.get("quantities") or {}
    body = email.get("body") or ""
    return {
        "uid": str(rec.get("uid")),
        "subject": email.get("subject"), "sender": email.get("sender"), "date": email.get("date"),
        "attachments": [a.get("filename") if isinstance(a, dict) else a for a in email.get("attachments") or []],
        "body": body[:8000] + ("\n[… email truncated]" if len(body) > 8000 else ""),
        "requirement": res.get("requirement") or {},
        "checks": [{"severity": c.get("severity"), "title": c.get("title"), "detail": _short(c.get("detail"), 300)} for c in res.get("checks") or []],
        "quantity_sheet": {"filename": q.get("filename"), "sheet": q.get("sheet"), "lines": len(q.get("lines") or []),
                           "grand_total_sqm": q.get("grand_total_sqm")} if q else None,
    }


# ── Item codes ─────────────────────────────────────────────────────────────────────────────────


def _master_entries() -> list[dict]:
    from ..itemcodes import api as ic_api

    data = ic_api.master()
    if not data.get("ready"):
        raise ToolError(data.get("message") or "The ERP item master isn't loaded.")
    return data["entries"]


def search_item_codes(query: str) -> dict:
    from ..itemcodes import master as M

    entries = _master_entries()
    q = (query or "").strip()
    if not q:
        raise ToolError("Give a code or some words to search for.")
    qu = q.upper()
    words = M.tokens(q)
    hits = []
    for e in entries:
        code, desc = e["code"].upper(), e["description"].upper()
        if qu in code or qu in desc:
            score = 3 if qu == code else 2
        else:
            common = words & set(e.get("tokens") or M.tokens(e["title"]))
            if not common:
                continue
            score = len(common) / max(len(words), 1)
        hits.append((score, e))
    hits.sort(key=lambda h: (-h[0], h[1]["row"]))
    items = [{"code": e["code"], "row": e["row"], "sl_no": e["sl_no"], "title": e["title"],
              "description": _short(e["description"], 300), "issues": e.get("issues") or []} for _, e in hits]
    return {"query": q, "master_rows": len(entries)} | _cap(items, 30)


def item_code_issues() -> dict:
    from ..itemcodes import api as ic_api

    d = ic_api.duplicates()
    if not d.get("ready"):
        raise ToolError(d.get("message") or "The ERP item master isn't loaded.")
    groups = [{"kind": g["kind"], "codes": g["codes"], "note": g["note"],
               "members": [{"code": m["code"], "text": _short(m["text"], 140), "rows": m["rows"]} for m in g["members"]]}
              for g in d["groups"]]
    misplaced = [{k: (_short(v, 140) if isinstance(v, str) else v) for k, v in x.items()} for x in d["misplaced"]]
    return {"rows": d["rows"], "codes": d["codes"], "counts": d["counts"], "notes": d["notes"],
            "groups": _cap(groups, 50), "misplaced": _cap(misplaced, 30),
            "kinds": {"repeated": "same code and text on more than one row",
                      "same_code_different_text": "one code used for two different items",
                      "near_duplicate": "different codes with near-identical wording",
                      "misplaced": "a variant code whose text reads like another item"}}


# ── Vendors ────────────────────────────────────────────────────────────────────────────────────


def search_vendors(query: str | None = None, category: str | None = None, missing_contact_only: bool | None = None) -> dict:
    from ..vendors import store

    vs = store.list_vendors(category=category or None, q=query or None)
    out = []
    for v in vs:
        missing = [k for k in ("email", "phone", "gstin", "contact_person") if not getattr(v, k)]
        has_contact = bool(v.email or v.phone)
        if missing_contact_only and has_contact:
            continue
        out.append({"id": v.id, "name": v.name, "categories": v.categories, "categories_to_confirm": v.categories_to_confirm,
                    "status": v.status, "city": v.city, "contact_person": v.contact_person, "email": v.email, "phone": v.phone,
                    "gstin": v.gstin, "has_contact": has_contact, "missing": missing, "projects": v.projects[:6],
                    "performance": store.performance_summary(v.performance)["label"], "notes": _short(v.notes, 200)})
    return {"filters": {"query": query, "category": category, "missing_contact_only": bool(missing_contact_only)},
            "categories": store.categories()} | _cap(out, 50)


def vendor_shortlist(category: str) -> dict:
    from ..shortlist import api as sl_api

    d = _call(sl_api.shortlist, category=category, codes=None)
    vendors = [{"rank": v["rank"], "name": v["name"], "status": v["status"], "score": v["score"],
                "coverage_pct": round(v["coverage"] * 100), "pre_ticked": v["selected"], "tied": v["tied"],
                "factors": [{"factor": f["label"], "weight": f["weight"], "score": f["score"], "reason": f["reason"]} for f in v["factors"]],
                "notes": v["notes"]} for v in d["vendors"]]
    return {"category": d["category"], "headline": d["headline"], "item_codes": d["item_codes"], "weights": d["weights"],
            "median_rates": d["medians"], "min_rfq": d.get("min_rfq"), "excluded": d["excluded"], "notes": d["notes"],
            "vendors": _cap(vendors, 30)}


# ── Quotes ─────────────────────────────────────────────────────────────────────────────────────


def list_quotes(vendor: str | None = None) -> dict:
    from ..quotes import api as q_api

    d = _call(q_api.list_quotes)
    if not d.get("ready"):
        raise ToolError(d.get("message") or "No quotes are on file.")
    items = d["items"]
    if vendor:
        v = vendor.strip().lower()
        items = [q for q in items if v in q["vendor"].lower() or v in q["file"].lower()]
        if not items:
            raise ToolError(f"No quote from a vendor matching '{vendor}'. Vendors with quotes: "
                            + ", ".join(q["vendor"] for q in d["items"]))
    out = [{"vendor": q["vendor"], "file": q["file"], "date": q["date"], "read_confidence": q["read_confidence"],
            "needs_vision": q["needs_vision"], "quantity_basis": q["quantity_basis"],
            "terms": [{"key": t["key"], "value": t["value"], "page": t["page"], "text": _short(t["text"], 200)} for t in q["terms"]],
            "lines": [{"sl_no": ln["sl_no"], "code": ln["code"], "description": _short(ln["description"], 120), "unit": ln["unit"],
                       "unit_as_written": ln["unit_as_written"], "qty": ln["qty"], "rate": ln["rate"], "amount": ln["amount"],
                       "page": ln["page"], "arithmetic_ok": ln["arithmetic_ok"]} for ln in q["lines"]],
            "notes": q["notes"]} for q in items]
    return {"quantity_basis_meaning": {"tender": "priced on the tender quantities", "different": "priced on its own quantities",
                                       "unknown": "couldn't tell"},
            "rates": "rupees per unit, before GST"} | _cap(out, 20)


def quote_comparison(basis: str = "tender") -> dict:
    from ..quotes import api as q_api

    d = _call(q_api.comparison, basis="quoted" if basis == "quoted" else "tender")
    if not d.get("ready"):
        raise ToolError(d.get("message") or "No quotes are on file.")
    vendors = [{"vendor": v["vendor"], "file": v["file"], "rank_repriced": v["rank_repriced"], "rank_as_quoted": v["rank_as_quoted"],
                "repriced_total": v["repriced_total"], "repriced_total_inr": _inr(v["repriced_total"]), "repriced_total_lakh": _lakh(v["repriced_total"]),
                "as_quoted_total": v["as_quoted_total"], "as_quoted_total_inr": _inr(v["as_quoted_total"]),
                "pct_above_lowest": v["pct_above_lowest"], "complete": v["complete"], "quantity_basis": v["quantity_basis"],
                "read_confidence": v["read_confidence"], "needs_vision": v["needs_vision"], "flags": v["flags"],
                "lines": [{"sl_no": ln["sl_no"], "code": ln["code"], "rate": ln["rate"], "rate_per_tender_unit": ln["rate_per_tender_unit"],
                           "vendor_unit": ln["vendor_unit"], "vendor_qty": ln["vendor_qty"], "repriced": ln["repriced"],
                           "comparable": ln["comparable"], "flags": ln["flags"]} for ln in v["lines"]]} for v in d["vendors"]]
    tender = [{"sl_no": t["sl_no"], "code": t["code"], "description": _short(t["description"], 100), "unit": t["unit"], "qty": t["qty"],
               "measured": t["measured"], "note": t["note"]} for t in d["tender"]]
    return {"basis": d["basis"], "basis_meaning": "tender = every quote re-priced on the same tender quantities (comparable lines only); "
            "quoted = each vendor's own PDF totals", "insights": d["insights"], "notes": d["notes"],
            "comparable_codes": d["comparable_codes"], "non_comparable": d["non_comparable"], "tender": tender, "vendors": vendors}


# ── Registry ───────────────────────────────────────────────────────────────────────────────────


def _schema(props: dict) -> dict:
    # Strict mode: every property listed in required; optional ones accept null.
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_OPT_STR = {"type": ["string", "null"]}

TOOLS: list[dict] = [
    {"name": "list_requirements", "fn": list_requirements,
     "description": "List requirement emails (cases) with stage, headline, open counts, when the last reply went to the site, "
                    "approval date and every RFQ sent (vendor, category, date). Stages: item_codes (codes to settle), waiting_on_site "
                    "(reply sent, waiting on the site), approve (items to approve), vendors (pick vendors), rfq (RFQs to send), rfq_sent "
                    "(waiting for quotes), quotes (quotes in: compare and choose the vendor), hod (vendor chosen: email the HOD / "
                    "awaiting the HOD), done (HOD approved), error (the requirement couldn't be opened).",
     "parameters": _schema({"stage": {"type": ["string", "null"], "enum": [*STAGES, None], "description": "Only this stage; null for all."}})},
    {"name": "get_requirement", "fn": get_requirement,
     "description": "One requirement in full: lines with matched ERP item codes, issues and decisions, email checks, replies sent to "
                    "the site, approval, vendor picks, RFQs sent, and quotes/award/HOD state when present.",
     "parameters": _schema({"uid": {"type": "string", "description": "Requirement id, e.g. 1010 or m42."}})},
    {"name": "read_email", "fn": read_email,
     "description": "The original requirement email (subject, sender, date, body, attachment names), the requirement extracted from it, "
                    "the intake checks and the quantity sheet summary.",
     "parameters": _schema({"uid": {"type": "string", "description": "Requirement id, e.g. 1010."}})},
    {"name": "search_item_codes", "fn": search_item_codes,
     "description": "Search the ERP item master by code (e.g. WP.02) or words (e.g. 'toilet wall'). Returns code, sheet row, "
                    "description and any duplicate/misplaced issue on that row.",
     "parameters": _schema({"query": {"type": "string"}})},
    {"name": "item_code_issues", "fn": item_code_issues,
     "description": "Problems in the ERP item master: repeated rows, one code with two meanings, near-duplicate codes, and "
                    "variant codes whose text reads like another item (misplaced).",
     "parameters": _schema({})},
    {"name": "search_vendors", "fn": search_vendors,
     "description": "Search the vendor directory by name/text and/or category. Each vendor has contact details, which of "
                    "email/phone/GSTIN/contact person is missing, status, projects and past performance. Also lists all categories.",
     "parameters": _schema({"query": _OPT_STR, "category": {"type": ["string", "null"], "description": "e.g. Waterproofing"},
                            "missing_contact_only": {"type": ["boolean", "null"], "description": "Only vendors with no email and no phone."}})},
    {"name": "vendor_shortlist", "fn": vendor_shortlist,
     "description": "The deterministic vendor ranking for a category: score, data coverage, pre-ticked for the RFQ, and per-factor "
                    "reasons: price vs the median, quality, on-time delivery, credit terms, lead time, response time and experience.",
     "parameters": _schema({"category": {"type": "string", "description": "e.g. Waterproofing"}})},
    {"name": "list_quotes", "fn": list_quotes,
     "description": "Vendor quotation PDFs as read: vendor, file, date, line items (code, unit, qty, rate before GST, amount, page), "
                    "terms (GST, payment, validity, lead time, warranty, with page and verbatim text) and quantity basis.",
     "parameters": _schema({"vendor": {"type": ["string", "null"], "description": "Part of a vendor name; null for all."}})},
    {"name": "quote_comparison", "fn": quote_comparison,
     "description": "The comparative statement: every quote re-priced on the tender quantities (basis 'tender') or on its own totals "
                    "('quoted'), with ranks, % above lowest, line-level flags, lines that can't be compared, and insights.",
     "parameters": _schema({"basis": {"type": "string", "enum": ["tender", "quoted"]}})},
]

BY_NAME = {t["name"]: t for t in TOOLS}


def openai_tools() -> list[dict]:
    """Function tools for the OpenAI Responses API, strict schemas."""
    return [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"], "strict": True}
            for t in TOOLS]


def run(name: str, args: dict) -> tuple[str, bool]:
    """Run a tool. Returns (JSON text, is_error)."""
    t = BY_NAME.get(name)
    if t is None:
        return json.dumps({"error": f"No tool called {name}."}), True
    try:
        result = t["fn"](**{k: v for k, v in (args or {}).items() if k in t["parameters"]["properties"]})
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except Exception as e:  # a bad record must not end the chat: tell the model what failed
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
    text = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + ' …[truncated: result too long; ask a narrower question]'
    return text, False


def summary(name: str, args: dict) -> str:
    """'get_requirement(1010)' for the 'looked at' line under an answer."""
    vals = [str(v) for v in (args or {}).values() if v not in (None, "", False)]
    return f"{name}({', '.join(vals)})" if vals else name
