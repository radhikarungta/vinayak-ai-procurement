"""The admin login: pages redirect and the API answers 401 until signed in; the cookie can't be forged."""

import pytest
from fastapi.testclient import TestClient

from procurement import auth
from procurement.api import app


@pytest.fixture
def cl(monkeypatch):
    monkeypatch.setenv("AUTH", "on")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    return TestClient(app, follow_redirects=False)


def test_signed_out(cl):
    assert cl.get("/").status_code == 303 and cl.get("/").headers["location"] == "/login"
    assert cl.get("/api/status").status_code == 401
    assert cl.get("/login").status_code == 200 and cl.get("/web/cases.js").status_code == 200


def test_wrong_password(cl):
    r = cl.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401 and "Wrong username or password" in r.text and auth.COOKIE not in r.cookies


def test_sign_in_and_out(cl):
    r = cl.post("/login", data={"username": "admin", "password": "s3cret"})
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    assert cl.get("/api/status").status_code == 200
    cl.get("/logout")
    assert cl.get("/api/status").status_code == 401


def test_forged_cookie(cl):
    cl.cookies.set(auth.COOKIE, "admin|9999999999|deadbeef")
    assert cl.get("/api/status").status_code == 401
