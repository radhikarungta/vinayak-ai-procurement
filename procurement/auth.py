"""A small admin login in front of the whole app. No database: one user from the environment.

ADMIN_USER / ADMIN_PASSWORD (default admin / admin: change them before the app is public).
SESSION_SECRET signs the session cookie (defaults to one derived from the password, so it stays the
same across restarts and instances). AUTH=off turns the login off (the tests do).

Signed out: pages go to /login, the API answers 401 (the page then goes to /login).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "vp_session"
MAX_AGE = 7 * 24 * 3600
OPEN = ("/login", "/logout", "/web/", "/favicon")  # reachable signed out (the scripts hold no data)

router = APIRouter()


def enabled() -> bool:
    return os.environ.get("AUTH", "on").lower() not in ("off", "0", "false")


def _user() -> str:
    return os.environ.get("ADMIN_USER") or "admin"


def _password() -> str:
    return os.environ.get("ADMIN_PASSWORD") or "admin"


def _secret() -> bytes:
    s = os.environ.get("SESSION_SECRET") or "vinayak:" + _user() + ":" + _password()
    return hashlib.sha256(s.encode()).digest()


def _sign(user: str, exp: int) -> str:
    mac = hmac.new(_secret(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}|{exp}|{mac}"


def signed_in(request: Request) -> bool:
    tok = request.cookies.get(COOKIE) or ""
    try:
        user, exp, mac = tok.rsplit("|", 2)
        ok = hmac.compare_digest(_sign(user, int(exp)), tok)
    except ValueError:
        return False
    return ok and user == _user() and int(exp) > time.time()


async def middleware(request: Request, call_next):
    path = request.url.path
    if not enabled() or path.startswith(OPEN) or signed_in(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Vinayak Procurement</title>
<script>try{const t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{--bg:#f3f5f6;--surface:#fff;--line:#d3dbdf;--fg:#17232b;--muted:#5a6a74;--accent:#0d6672;--on-accent:#fff;--bad:#b3261e;--bad-soft:#f9dedc}
:root[data-theme=dark]{--bg:#0f171c;--surface:#16222a;--line:#2c3d47;--fg:#e3eaee;--muted:#93a5af;--accent:#4fb8c4;--on-accent:#0b1a1e;--bad:#f28b82;--bad-soft:#3d1c1a}
*{box-sizing:border-box} body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
form{width:min(360px,calc(100vw - 32px));background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:24px;display:flex;flex-direction:column;gap:12px}
.brand{display:flex;gap:10px;align-items:center;margin-bottom:6px} .mark{width:34px;height:34px;border-radius:8px;background:var(--accent);color:var(--on-accent);display:grid;place-items:center;font-weight:800}
.brand b{display:block} .brand span{font-size:.8rem;color:var(--muted)}
label{font-size:.85rem;font-weight:500;display:flex;flex-direction:column;gap:4px}
input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg)}
button{font:inherit;font-weight:600;cursor:pointer;padding:9px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent)}
.err{padding:8px 10px;border-radius:6px;background:var(--bad-soft);color:var(--bad);font-size:.88rem}
</style></head><body>
<form method="post" action="/login">
  <div class="brand"><span class="mark">V</span><div><b>Vinayak Procurement</b><span>Admin sign in</span></div></div>
  {error}
  <label>Username <input name="username" autocomplete="username" required autofocus></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in</button>
</form></body></html>"""


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not enabled() or signed_in(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(PAGE.replace("{error}", ""))


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ok = hmac.compare_digest(username.encode(), _user().encode()) & hmac.compare_digest(password.encode(), _password().encode())
    if not ok:
        return HTMLResponse(PAGE.replace("{error}", '<div class="err" role="alert">Wrong username or password.</div>'), status_code=401)
    exp = int(time.time()) + MAX_AGE
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(COOKIE, _sign(username, exp), max_age=MAX_AGE, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return r


@router.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r
