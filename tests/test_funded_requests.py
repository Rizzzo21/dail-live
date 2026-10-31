"""Funded Request board (deep-dive Phase C): post with escrow, bid, pick,
submit, accept (winner net + remainder refund), reject, cancel, expiry."""
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
    return f"{prefix}_fr{_seq[0]:04d}"


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


def _post(poster, budget=20):
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d",
        "budget": budget, "acceptance_criteria": "Check: works."},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bid(bidder, rid, price):
    r = client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": bidder, "price": price, "days": 7, "note": "can do"},
        headers=_auth(bidder))
    assert r.status_code == 200, r.text


def test_full_flow_winner_net_and_remainder_refund():
    poster, winner, loser = _uid("p"), _uid("w"), _uid("l")
    for x in (poster, winner, loser):
        _make(x)
    pb, wb = _bal(poster), _bal(winner)
    rid = _post(poster, budget=20)
    assert _bal(poster) == pb - 20  # escrowed at posting
    _bid(winner, rid, 12)
    _bid(loser, rid, 15)
    r = client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "picked"
    r = client.post(f"/world/requests/{rid}/submit", json={
        "agent_id": winner, "submission": "x" * 150}, headers=_auth(winner))
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    r = client.post(f"/world/requests/{rid}/accept", json={"agent_id": poster},
                    headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    # winning bid 12, fee max(1, 12*1000//10000)=1, winner nets 11
    assert _bal(winner) == wb + 11
    # poster gets the unspent remainder 20-12=8 back
    assert _bal(poster) == pb - 20 + 8


def test_bid_rules_reject_and_cancel():
    poster, bidder = _uid("p"), _uid("b")
    for x in (poster, bidder):
        _make(x)
    rid = _post(poster, budget=10)
    # over budget
    r = client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": bidder, "price": 11}, headers=_auth(bidder))
    assert r.status_code == 400
    # poster cannot bid on own request
    r = client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": poster, "price": 5}, headers=_auth(poster))
    assert r.status_code == 400
    _bid(bidder, rid, 8)
    # one bid per agent
    r = client.post(f"/world/requests/{rid}/bid", json={
        "agent_id": bidder, "price": 7}, headers=_auth(bidder))
    assert r.status_code == 400
    # pick a non-bidder fails
    r = client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": _uid("nobody")}, headers=_auth(poster))
    assert r.status_code == 404
    # cancel refunds the full budget
    pb = _bal(poster)
    r = client.post(f"/world/requests/{rid}/cancel", json={"agent_id": poster},
                    headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _bal(poster) == pb + 10


def test_reject_reopens_and_bars_winner():
    poster, winner, other = _uid("p"), _uid("w"), _uid("o")
    for x in (poster, winner, other):
        _make(x)
    rid = _post(poster, budget=10)
    _bid(winner, rid, 9)
    _bid(other, rid, 8)
    client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    client.post(f"/world/requests/{rid}/submit", json={
        "agent_id": winner, "submission": "x" * 150}, headers=_auth(winner))
    r = client.post(f"/world/requests/{rid}/reject", json={"agent_id": poster},
                    headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "open"
    assert winner in r.json()["barred_bidders"]
    # barred winner cannot be re-picked; the other bidder can
    r = client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": winner}, headers=_auth(poster))
    assert r.status_code == 400
    r = client.post(f"/world/requests/{rid}/pick", json={
        "agent_id": poster, "bidder_id": other}, headers=_auth(poster))
    assert r.status_code == 200
