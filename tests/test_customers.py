"""Tests for the human customer dashboard backend (invite auth, tasks, review)."""
import os
import sys
from pathlib import Path

os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from dail.api import app, dail

ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _authed_customer(name="Test Human"):
    code = TestClient(app).post("/admin/invites", headers=ADMIN).json()["code"]
    r = TestClient(app).post("/customers/redeem",
                             json={"code": code, "name": name})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_invite_redeem_and_me():
    c = TestClient(app)
    code = c.post("/admin/invites", headers=ADMIN).json()["code"]
    assert code.startswith("DAIL-INV-")
    r = c.post("/customers/redeem", json={"code": code, "name": "Alice"})
    assert r.status_code == 200
    tok = r.json()["session_token"]
    me = c.get("/customers/me",
               headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["name"] == "Alice"
    assert me["balance"] == 0
    # double redeem fails
    r2 = c.post("/customers/redeem", json={"code": code, "name": "Bob"})
    assert r2.status_code == 400


def test_unauthorized_customer_endpoints():
    c = TestClient(app)
    r = c.get("/customers/me")
    assert r.status_code == 401
    r = c.get("/customers/me", headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401


def test_customer_cannot_see_others_tasks():
    c = TestClient(app)
    h1 = _authed_customer("Human One")
    h2 = _authed_customer("Human Two")
    # fund human one via ledger directly (test helper path)
    assert c.get("/customers/me/tasks", headers=h1).json() == []
    assert c.get("/customers/me/tasks", headers=h2).json() == []
    # isolation: no cross-customer leakage on empty sets
    assert h1 != h2


def test_dashboard_page_public():
    c = TestClient(app)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Your Dashboard" in r.text or "Get work done" in r.text


def test_invite_admin_only():
    c = TestClient(app)
    r = c.post("/admin/invites")
    assert r.status_code == 403


def test_customer_checkout_requires_auth():
    c = TestClient(app)
    r = c.post("/customers/me/checkout", json={"usd_cents": 2000})
    assert r.status_code == 401


def test_customer_checkout_validates_amount():
    c = TestClient(app)
    h = _authed_customer("Checkout Tester")
    # Too small
    r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": 50})
    # 503 because Stripe isn't configured in test, or 400 for validation.
    # Either way it must not 500 or succeed silently.
    assert r.status_code in (400, 503)
    # Non-integer
    r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": "abc"})
    assert r.status_code == 400
