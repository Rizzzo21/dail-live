"""bnty_0133: customer identities, invitations, sessions, and task ownership
must survive a process restart with zero loss.

This is the in-repo, always-runnable proof: it drives the REAL customer API
(TestClient over dail.api) and then fully tears the service down and rebuilds
it — a brand-new Dail() against the same database plus a fresh app binding —
before reading the data back. No dict is copied between generations; the
second generation can only see what the first one persisted.

The stronger, out-of-process proof (two real uvicorn processes over HTTP
against a real PostgreSQL 16 server) lives in
scratch/autonomous-income/airdrop-farm/bnty0133_restart_proof.py and its
captured output is attached to the bounty submission.
"""
import os
import sys
from pathlib import Path

os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import dail.api as api_mod
from dail.service import Dail

ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _generation(db_url):
    """Build a fresh service + app against db_url. Returns (dail, client).

    The module-global `dail` is rebound so every route/middleware resolves
    against THIS generation — the previous generation is discarded, not
    copied. Callers must go through the `_restore_globals` fixture so the
    original module global is put back afterwards (otherwise later test files
    would run against this throwaway instance).
    """
    os.environ["DATABASE_URL"] = db_url
    d = Dail()
    api_mod.dail = d
    return d, TestClient(api_mod.app)


@pytest.fixture(autouse=True)
def _restore_globals():
    """Snapshot and restore dail.api's module globals around every test here.

    Rebinding api_mod.dail is what makes the restart real (routes resolve
    against the new instance), but it is process-global state: without this,
    the last generation would leak into every later test module.
    """
    saved_dail = api_mod.dail
    saved_url = os.environ.get("DATABASE_URL")
    yield
    api_mod.dail = saved_dail
    if saved_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = saved_url


def _snapshot(c, headers):
    """Everything a customer owns, read back through the public API."""
    me = c.get("/customers/me", headers=headers).json()
    tasks = c.get("/customers/me/tasks", headers=headers).json()
    return {
        "id": me["id"],
        "name": me["name"],
        "email": me["email"],
        "verified": me["verified"],
        "created_at": me["created_at"],
        "balance": me["balance"],
        "tasks": sorted((t["id"], t["title"], t["reward"], t["status"]) for t in tasks),
    }


def _run_round_trip(db_url):
    """Write -> restart -> read. Returns (before, after) snapshots + evidence."""
    # ---- generation 1: register a customer and post a funded task ----
    d1, c1 = _generation(db_url)
    assert d1.store.enabled, "persistence must be enabled for this proof"
    code = c1.post("/admin/invites", headers=ADMIN).json()["code"]
    redeem = c1.post("/customers/redeem",
                     json={"code": code, "name": "Ada Restart",
                           "email": "ada@example.com"}).json()
    tok = redeem["session_token"]
    cid = redeem["customer_id"]
    H = {"Authorization": f"Bearer {tok}"}
    # fund the customer, then post a task (real escrow through the bounty path)
    c1.post(f"/admin/customers/{cid}/credit", headers=ADMIN, json={"amount": 100})
    posted = c1.post("/customers/me/tasks", headers=H,
                     json={"title": "Restart-proof task", "description": "survive me",
                           "reward": 25})
    assert posted.status_code == 200, posted.text
    bounty_id = posted.json()["id"]
    before = _snapshot(c1, H)
    assert before["name"] == "Ada Restart"
    assert before["balance"] == 100 - 25  # 100 credited, 25 escrowed
    assert before["tasks"] == [(bounty_id, "Restart-proof task", 25, "open")]
    # an invite was consumed; its used-mark must also survive
    invites_before = {i["code"]: i["used_by"] for i in
                      c1.get("/admin/invites", headers=ADMIN).json()}
    assert invites_before[code] == cid

    # ---- full teardown: drop every object from generation 1 ----
    del c1
    del d1

    # ---- generation 2: brand-new service + app against the same DB ----
    d2, c2 = _generation(db_url)
    # the rebuilt service must have restored the customer from the database
    assert cid in d2.customers.customers, "customer not restored from store"
    after = _snapshot(c2, H)  # same bearer token, new process/app
    invites_after = {i["code"]: i["used_by"] for i in
                     c2.get("/admin/invites", headers=ADMIN).json()}
    return before, after, invites_before, invites_after, code, cid, bounty_id


@pytest.fixture()
def sqlite_db(tmp_path):
    old = os.environ.get("DATABASE_URL")
    yield f"sqlite:///{tmp_path}/customer_restart.db"
    if old is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old


def test_customer_state_survives_restart(sqlite_db):
    before, after, inv_b, inv_a, code, cid, bid = _run_round_trip(sqlite_db)
    # ZERO LOSS: identity, balance, session, task ownership all identical.
    assert after == before, f"customer state changed across restart:\n{before}\n{after}"
    # session token from generation 1 still authenticates in generation 2
    assert after["id"] == cid
    # consumed invite stayed consumed: the code cannot be redeemed again
    assert inv_a == inv_b
    assert inv_a[code] == cid


def test_redeem_is_rejected_after_restart(sqlite_db):
    """A used invite must not become reusable just because the process
    restarted (the in-memory-only bug this fixes)."""
    d1, c1 = _generation(sqlite_db)
    code = c1.post("/admin/invites", headers=ADMIN).json()["code"]
    c1.post("/customers/redeem", json={"code": code, "name": "First"})
    del c1, d1
    d2, c2 = _generation(sqlite_db)
    r = c2.post("/customers/redeem", json={"code": code, "name": "Second"})
    assert r.status_code == 400, "used invite was reusable after restart"
    # a brand-new invite still works and does not collide with restored ids
    code2 = c2.post("/admin/invites", headers=ADMIN).json()["code"]
    r2 = c2.post("/customers/redeem", json={"code": code2, "name": "Second"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["customer_id"] != "cust_0001"
