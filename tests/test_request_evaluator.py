"""Independent evaluator (deep-dive Phase D): a third agent may accept or
reject a request submission; the evaluator cannot bid or be the poster."""
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
    return f"{prefix}_ev{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _setup(evaluator=True):
    poster, winner, ev = _uid("p"), _uid("w"), _uid("e")
    for x in (poster, winner, ev):
        _make(x)
    body = {"agent_id": poster, "title": "t", "description": "d", "budget": 10}
    if evaluator:
        body["evaluator_id"] = ev
    r = client.post("/world/requests", json=body, headers=_auth(poster))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": winner, "price": 9}, headers=_auth(winner))
    client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    client.post(f"/world/requests/{rid}/submit", json={
        "agent_id": winner, "submission": "x" * 150}, headers=_auth(winner))
    return poster, winner, ev, rid


def test_evaluator_accepts_and_outsider_blocked():
    poster, winner, ev, rid = _setup()
    # outsider cannot accept
    outsider = _uid("o")
    _make(outsider)
    r = client.post(f"/world/requests/{rid}/accept", json={"agent_id": outsider},
                    headers=_auth(outsider))
    assert r.status_code == 403
    # evaluator can
    r = client.post(f"/world/requests/{rid}/accept", json={"agent_id": ev},
                    headers=_auth(ev))
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_evaluator_rejects_and_cannot_bid():
    poster, winner, ev, rid = _setup()
    r = client.post(f"/world/requests/{rid}/reject", json={"agent_id": ev},
                    headers=_auth(ev))
    assert r.status_code == 200 and r.json()["status"] == "open"
    # evaluator cannot bid on a request they judge (new request)
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": 10,
        "evaluator_id": ev}, headers=_auth(poster))
    rid2 = r.json()["id"]
    r = client.post(f"/world/requests/{rid2}/bid", json={
        "agent_id": ev, "price": 5}, headers=_auth(ev))
    assert r.status_code == 400
    # poster cannot be their own evaluator
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": 10,
        "evaluator_id": poster}, headers=_auth(poster))
    assert r.status_code == 400
