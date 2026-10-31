"""Request review SLA (deep-dive fix, 2026-10-30): a submitted request
with no poster/evaluator decision for 7 days auto-accepts on the lazy
read path — winner nets price minus the 10% fee (no first-3 waiver),
unspent remainder refunds to the poster. Open/picked requests still
expire-and-refund; a banned winner never auto-accepts."""
import os
import sys
from datetime import datetime, timedelta, timezone
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
    return f"{prefix}_sla{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    r = client.get(f"/ledger/{aid}", headers=_auth(aid))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def _submitted_request(poster, winner, budget=20, price=12):
    _make(poster)
    _make(winner)
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": budget},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": winner, "price": price, "days": 7, "note": "can do"},
        headers=_auth(winner))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/requests/{rid}/submit", json={
        "agent_id": winner, "submission": "x" * 150}, headers=_auth(winner))
    assert r.status_code == 200, r.text
    return rid


def _backdate_submitted(rid, days):
    past = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    dail.world_agents.requests[rid]["submitted_at"] = past


def test_auto_accept_after_7_days_pays_winner_and_refunds_remainder():
    poster, winner = _uid("p"), _uid("w")
    rid = _submitted_request(poster, winner)
    pb, wb = _bal(poster), _bal(winner)
    _backdate_submitted(rid, 8)
    r = client.get(f"/world/requests/{rid}", headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    # price 12, fee max(1, 12*1000//10000)=1, winner nets 11; NO waiver
    assert _bal(winner) == wb + 11
    # poster gets the unspent remainder 20-12=8 back
    assert _bal(poster) == pb + 8


def test_no_auto_accept_before_7_days():
    poster, winner = _uid("p"), _uid("w")
    rid = _submitted_request(poster, winner)
    _backdate_submitted(rid, 6)
    r = client.get(f"/world/requests/{rid}", headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"


def test_picked_request_still_expires_and_refunds():
    poster, winner = _uid("p"), _uid("w")
    _make(poster)
    _make(winner)
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": 10},
        headers=_auth(poster))
    rid = r.json()["id"]
    client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": winner, "price": 8, "days": 7}, headers=_auth(winner))
    client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    pb = _bal(poster)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    dail.world_agents.requests[rid]["listing_expires_at"] = past
    r = client.get(f"/world/requests/{rid}", headers=_auth(poster))
    assert r.json()["status"] == "expired"
    assert _bal(poster) == pb + 10


def test_banned_winner_never_auto_accepts():
    poster, winner = _uid("p"), _uid("w")
    rid = _submitted_request(poster, winner)
    _backdate_submitted(rid, 8)
    dail.world_agents.banned_ids.add(winner)
    try:
        r = client.get(f"/world/requests/{rid}", headers=_auth(poster))
        assert r.json()["status"] == "submitted"
    finally:
        dail.world_agents.banned_ids.discard(winner)
