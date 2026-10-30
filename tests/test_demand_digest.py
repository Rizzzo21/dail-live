"""Demand digest (deep-dive Phase I): open funded requests, expiring
claims, unread count, subscriptions due — own digest only."""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_dg{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_digest_contents_and_ownership():
    poster, hunter, prov = _uid("p"), _uid("h"), _uid("pv")
    for x in (poster, hunter, prov):
        _make(x)
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "need work", "description": "d", "budget": 15},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster))
    bid = r.json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={"agent_id": hunter},
                headers=_auth(hunter))
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": 5},
        headers=_auth(prov))
    sid = r.json()["id"]
    client.post("/world/subscriptions", json={
        "agent_id": hunter, "service_id": sid, "interval_days": 7},
        headers=_auth(hunter))
    d = client.get(f"/world/digest/{hunter}", headers=_auth(hunter)).json()
    assert any(x["id"] == rid for x in d["open_requests"])
    assert any(x["bounty_id"] == bid for x in d["expiring_claims"])
    assert d["expiring_claims"][0]["claim_seconds_remaining"] is not None
    assert len(d["subscriptions_due"]) == 1
    # another agent cannot read this digest
    r = client.get(f"/world/digest/{hunter}", headers=_auth(poster))
    assert r.status_code == 403
