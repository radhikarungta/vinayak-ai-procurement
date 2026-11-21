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


def load(uid: str, table: str = TABLE, col: str = "state") -> dict | None:
    """The saved row's JSON (the state, by default), or None if there is none yet."""
    with _client() as c:
        r = c.get(f"/{table}", params={"uid": f"eq.{uid}", "select": col})
    if r.status_code != 200:
        raise DBError(f"Supabase read failed ({r.status_code}): {r.text[:200]}")
    rows = r.json()
    return rows[0][col] if rows else None


def save(uid: str, state: dict, table: str = TABLE, col: str = "state") -> None:
    """Insert or replace the row's JSON (one row per requirement)."""
    with _client() as c:
        r = c.post(f"/{table}", params={"on_conflict": "uid"}, json={"uid": uid, col: state},
                   headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code not in (200, 201, 204):
        raise DBError(f"Supabase write failed ({r.status_code}): {r.text[:200]}")


def all_rows(table: str, col: str) -> list[dict]:
    """Every row's JSON in a table, oldest first (the simulated emails)."""
    with _client() as c:
        r = c.get(f"/{table}", params={"select": col, "order": "uid"})
    if r.status_code != 200:
        raise DBError(f"Supabase read failed ({r.status_code}): {r.text[:200]}")
    return [row[col] for row in r.json()]


def delete_all(table: str) -> None:
    with _client() as c:
        r = c.delete(f"/{table}", params={"uid": "neq."})
    if r.status_code not in (200, 204):
        raise DBError(f"Supabase delete failed ({r.status_code}): {r.text[:200]}")
