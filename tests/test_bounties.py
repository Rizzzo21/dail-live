import sys
import os
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
    return f"{prefix}_bn{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _treasury():
    return client.get("/treasury").json()["balance"]


def test_bounty_full_lifecycle():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    t0 = _treasury()
    # post: reward escrowed immediately
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Need a logo",
        "description": "SVG logo for an agent marketplace", "reward": 50},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["id"].startswith("bnty_") and b["status"] == "open"
    assert _bal(poster) == 50  # 100 - 50 held
    # public listing shows it
    r = client.get("/world/bounties")
    assert any(x["id"] == b["id"] for x in r.json()["bounties"])
    # claim
    r = client.post(f"/world/bounties/{b['id']}/claim", json={
        "agent_id": hunter, "submission": "<svg>...</svg>"}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "claimed"
    # accept: hunter nets 45, treasury takes 5 (10%)
    r = client.post(f"/world/bounties/{b['id']}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert _bal(hunter) == 145
    assert _treasury() == t0 + 5


def test_bounty_cancel_refunds():
    poster = _uid("p")
    _make(poster)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/cancel",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _bal(poster) == 100


def test_bounty_guards():
    poster, hunter, stranger = _uid("p"), _uid("h"), _uid("s")
    _make(poster); _make(hunter); _make(stranger)
    # reward too small
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 1},
        headers=_auth(poster))
    assert r.status_code == 400
    # unauthenticated
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10})
    assert r.status_code == 401
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster)).json()["id"]
    # cannot claim own bounty
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": poster, "submission": "mine"}, headers=_auth(poster))
    assert r.status_code == 400
    # stranger cannot accept
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "work"}, headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": stranger}, headers=_auth(stranger))
    assert r.status_code == 403
    # cannot claim twice
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": stranger, "submission": "late"}, headers=_auth(stranger))
    assert r.status_code == 400


def test_ban_hunter_voids_claim_reopens_bounty():
    # Banning the hunter of a claimed bounty: the claim dies, the bounty
    # reopens, escrow stays held for the next hunter (poster is innocent).
    from dail.api import dail as _d
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Need work",
        "description": "do the thing", "reward": 40},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "done"}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    _d.ban_agent(hunter, "test")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "open", b
    assert b.get("hunter_id") is None
    assert hunter in _d.world_agents.banned_ids
    # poster balance unchanged (escrow still held: 100 - 40)
    r = client.get(f"/ledger/{poster}", headers=_auth(poster))
    assert r.json()["balance"] == 60
    # unban clears the sweep guard
    _d.unban_agent(hunter)
    assert hunter not in _d.world_agents.banned_ids


def test_referral_reward_fires_on_bounty_track():
    # Referrers earn when their invitee's first real earning is a bounty,
    # not just a trade/order. Dust gate still applies (reward 25 >= 10).
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    referrer, hunter, poster = _uid("r"), _uid("h"), _uid("p")
    # distinct public registration IPs: the TestClient reports client.host as
    # "testclient" for all requests, which would trip the shared-IP wash guard
    def _mk(aid, ip, **kw):
        r = client.post("/agents", json={"id": aid, "name": aid, **kw},
                        headers={"X-Forwarded-For": ip})
        assert r.status_code == 200, r.text
        _keys[aid] = r.json()["api_key"]
    _mk(referrer, "9.80.0.1"); _mk(poster, "9.80.0.2")
    _mk(hunter, "9.80.0.3", referred_by=referrer)
    _d.world_agents.agent_created[poster] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _d.world_agents.agent_created[hunter] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Audit this",
        "description": "real work", "reward": 25},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "report"}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    bal_before = client.get(f"/ledger/{referrer}",
                            headers=_auth(referrer)).json()["balance"]
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    bal_after = client.get(f"/ledger/{referrer}",
                           headers=_auth(referrer)).json()["balance"]
    assert bal_after == bal_before + 10, (bal_before, bal_after)
