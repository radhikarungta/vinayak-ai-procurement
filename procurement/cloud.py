"""Hosting on Vercel: the git-ignored data (vendors, deals, item codes, the tender folder) comes from a
private Supabase Storage bucket, unpacked into /tmp when a server instance starts.

scripts/push_data.py packs data/ into `data/data.tar.gz` in the bucket. Requirement states and simulated
emails are in Supabase tables (procurement/cases/db.py), so every instance sees the same ones; edits to
the vendor directory or email rules on the hosted app last only as long as the instance (demo).

Locally (no VERCEL env) none of this runs: the app reads data/ as always.
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import httpx

BUCKET, OBJECT = "data", "data.tar.gz"
BASE = Path("/tmp/vinayak")  # the bundle unpacks to BASE / "data"


def _headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _url(path: str) -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1/" + path


def active() -> bool:
    return bool(os.environ.get("VERCEL") and os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def prepare() -> None:
    """Before the app is imported: unpack the data bundle once per instance and point every path at it."""
    if not active():
        return
    data = BASE / "data"
    if not (BASE / ".ready").exists():
        r = httpx.get(_url(f"object/{BUCKET}/{OBJECT}"), headers=_headers(), timeout=60)
        r.raise_for_status()
        BASE.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as t:
            t.extractall(BASE, filter="data")
        (BASE / ".ready").touch()
    for k, v in {"VENDORS_PATH": data / "vendors.json", "VENDORS_PACK": data / "pack", "VENDOR_DEALS_PATH": data / "vendor_deals.json",
                 "EMAIL_RULES_PATH": data / "email_rules.json", "QUOTES_PACK": data / "pack", "CASES_DIR": data / "cases",
                 "ITEM_MASTER_PATH": data / "pack" / "04_item_codes" / "ERP_item_codes_waterproofing.xlsx"}.items():
        os.environ.setdefault(k, str(v))


def patch() -> None:
    """After the app is imported: the few paths that are module constants rather than settings."""
    if not active():
        return
    from . import mailbox
    from .quotes import pack
    from .shortlist import deals

    data = BASE / "data"
    pack.SEED_PATH = data / "seed" / "parsed_quote_deals.json"
    deals.SEED_DIR = data / "seed"
    mailbox.STORE = data / "inbox"


def upload(tar_gz: bytes) -> None:
    """scripts/push_data.py: make the private bucket if needed and replace the bundle."""
    with httpx.Client(headers=_headers(), timeout=120) as c:
        r = c.post(_url("bucket"), json={"id": BUCKET, "name": BUCKET, "public": False})
        if r.status_code not in (200, 201) and "already exists" not in r.text.lower() and r.status_code != 409:
            raise RuntimeError(f"Bucket: {r.status_code} {r.text[:200]}")
        r = c.post(_url(f"object/{BUCKET}/{OBJECT}"), content=tar_gz,
                   headers={"x-upsert": "true", "Content-Type": "application/gzip"})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Upload: {r.status_code} {r.text[:200]}")
