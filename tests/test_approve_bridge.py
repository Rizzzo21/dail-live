"""tests/test_approve_bridge.py

bnty_0132 — approval-to-post bridge.

The bug: POST /admin/bounty-requests/{id}/review with status=approved only
flips the request status. No bounty is created, nothing is escrowed, nothing
reaches /world/bounties — an approved human request is a no-op forever.

Run against the UNPATCHED service: this test FAILS (gap reproduced).
Run against the patched service (dail/approve_bridge wired into
review_bounty_request): it PASSES.

Isolation: the API routes bind the module-level `dail` object in dail.api.
This module swaps it for a FRESH Dail() instance per test (restored
afterwards via monkeypatch), so it never mutates the shared test app that
other test files (e.g. test_bouncer.py) depend on.
"""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TESTING"] = "1"
os.environ["DAIL_REG_LIMIT"] = "10000"
os.environ["DAIL_VAULT_MAX_SUPPLY"] = "1000000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import dail.api as api_module  # noqa: E402
from dail.models import Agent  # noqa: E402
from dail.service import Dail  # noqa: E402
from conftest import fund_vault  # noqa: E402

_admin = {"X-DAIL-Admin-Key": "test-admin-key"}


@pytest.fixture()
def bridge(monkeypatch):
    """Fresh Dail() instance swapped in for dail.api's module-level `dail`."""
    d = Dail()
    fund_vault(d)
    monkeypatch.setattr(api_module, "dail", d)
    return d


def _client():
    return TestClient(api_module.app)


def _funder(d):
    fid = "dail_host"
    if fid not in d.social.identities:
        d.create_agent(Agent(id=fid, name="DAiL Host", goal="fund", balance=100000))
    return fid


def _submit(d, client, title, reward):
    r = client.post("/request-bounty", data={
        "title": title, "description": "Do the thing end to end, with test output.",
        "reward": str(reward), "contact": "human@example.com"})
    assert r.status_code == 200, r.text
    items = d.world_agents.list_bounty_requests()["requests"]
    m = [x for x in items if x["title"] == title]
    assert m
    return m[0]["id"]


def test_approve_via_admin_endpoint_posts_a_funded_bounty(bridge):
    """FIXED behavior: approving the request publishes a real, funded bounty."""
    d = bridge
    _funder(d)
    client = _client()
    before = len(d.world_agents.bounties)
    rid = _submit(d, client, "Bridge E2E: approve must post", 20)

    r = client.post(f"/admin/bounty-requests/{rid}/review", headers=_admin,
                    json={"status": "approved", "note": "ship it"})
    assert r.status_code == 200, r.text

    after = len(d.world_agents.bounties)
    assert after == before + 1, (
        f"approval created no bounty (board {before} -> {after}); the "
        f"approval-to-post bridge is missing")
    posted = [b for b in d.world_agents.bounties.values()
              if b["title"] == "Bridge E2E: approve must post"]
    assert len(posted) == 1
    b = posted[0]
    assert b["status"] == "open" and b["reward"] == 20
    assert d.ledger.balances.get(f"escrow:{b['id']}", 0) == 20

    req = d.world_agents.bounty_requests[rid]
    assert req.get("bounty_id") == b["id"]
    r2 = client.post(f"/admin/bounty-requests/{rid}/review", headers=_admin,
                     json={"status": "approved"})
    assert r2.status_code == 200
    assert len(d.world_agents.bounties) == before + 1, "double-posted on re-approve"


def test_dismiss_creates_nothing(bridge):
    d = bridge
    _funder(d)
    client = _client()
    before = len(d.world_agents.bounties)
    rid = _submit(d, client, "Bridge E2E: dismiss is inert", 15)
    r = client.post(f"/admin/bounty-requests/{rid}/review", headers=_admin,
                    json={"status": "dismissed", "note": "no"})
    assert r.status_code == 200
    assert len(d.world_agents.bounties) == before
