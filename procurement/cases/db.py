"""Where each requirement's state is kept: a Supabase table when it's configured, local files otherwise.

Set SUPABASE_URL and SUPABASE_SERVICE_KEY (the service-role key: server side only, never in the page)
to keep the state in the `case_state` table (supabase/migrations/001_case_state.sql). Without them
nothing changes: the state stays in data/cases/<uid>.json, as before.

Talks to Supabase's REST API (PostgREST) over httpx, so there is no extra dependency.
"""

from __future__ import annotations

import os

import httpx

TABLE = "case_state"


class DBError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def _client() -> httpx.Client:
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return httpx.Client(
        base_url=os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=10,
    )


def load(uid: str) -> dict | None:
    """The saved state, or None if this requirement has none yet."""
    with _client() as c:
        r = c.get(f"/{TABLE}", params={"uid": f"eq.{uid}", "select": "state"})
    if r.status_code != 200:
        raise DBError(f"Supabase read failed ({r.status_code}): {r.text[:200]}")
    rows = r.json()
    return rows[0]["state"] if rows else None


def save(uid: str, state: dict) -> None:
    """Insert or replace the whole state (one row per requirement)."""
    with _client() as c:
        r = c.post(f"/{TABLE}", params={"on_conflict": "uid"}, json={"uid": uid, "state": state},
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code not in (200, 201, 204):
        raise DBError(f"Supabase write failed ({r.status_code}): {r.text[:200]}")
