"""Funding split (deep-dive Phase E): /world/stats separates human-funded
(customer: shadow posters), agent-funded, and house-funded completions."""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_fs{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _complete_bounty(poster, hunter, reward, via_api=True):
    if via_api:
        r = client.post("/world/bounties", json={
            "agent_id": poster, "title": "t", "description": "d",
            "reward": reward}, headers=_auth(poster))
        assert r.status_code == 201, r.text
        bid = r.json()["id"]
        r = client.post(f"/world/bounties/{bid}/claim", json={
            "agent_id": hunter, "submission": "x" * 150}, headers=_auth(hunter))
        assert r.status_code == 200, r.text
        r = client.post(f"/world/bounties/{bid}/accept", json={
            "agent_id": poster}, headers=_auth(poster))
        assert r.status_code == 200, r.text
    else:
        # human leg: shadow customer identity, funded directly, settled
        # through the service (the customer dashboard route does the same
        # shadow registration in production).
        dail.social.identities[poster] = {"id": poster, "name": "Human"}
        dail.ledger.credit(poster, reward, kind="credit", idem=f"fs-{poster}")
        b = dail.world_agents.post_bounty(poster, "t", "d", reward)
        dail.world_agents.claim_bounty(hunter, b["id"], "x" * 150)
        dail.world_agents.accept_bounty(poster, b["id"])


def test_funding_split_human_vs_agent():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    stats0 = client.get("/world/stats").json()["funding"]
    _complete_bounty(poster, hunter, 10, via_api=True)
    human_poster = "customer:" + _uid("cust")
    _complete_bounty(human_poster, hunter, 20, via_api=False)
    f = client.get("/world/stats").json()["funding"]
    assert f["agent_completed"] == stats0["agent_completed"] + 1
    assert f["agent_dail_paid"] == stats0["agent_dail_paid"] + 10
    assert f["human_completed"] == stats0["human_completed"] + 1
    assert f["human_dail_paid"] == stats0["human_dail_paid"] + 20
    assert f["human_share_of_paid"] > 0
