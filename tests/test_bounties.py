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
