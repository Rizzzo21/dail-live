"""Commerce-gap fixes (follow-up audit, 2026-10-07): delivery SLA, dispute
auto-refund, bounty expiry, ratings, bounty edit/release, poster filter,
per-agent ledger history."""
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail
from dail import service as svc_mod

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_cg{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _service(provider, price=20, hours=72):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": hours}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _buy(buyer, sid):
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": _uid("k")}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def test_delivery_sla_and_overdue_nudge():
    prov, buyer = _uid("p"), _uid("b")
    for x in (prov, buyer):
        _make(x)
    sid = _service(prov, hours=72)
    oid = _buy(buyer, sid)
    d = dail
    order = d.world_agents.orders[oid]
    assert order["deliver_by"], "order should carry a deliver_by"
    assert "delivery_overdue" in client.get(
        f"/world/orders", headers=_auth(buyer)).json()["orders"][0]
    # time-travel past the window
    order["deliver_by"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    client.get("/world/orders", headers=_auth(buyer))  # sweep runs on read
    assert order["overdue_notified"] is True
    notifs = client.get(f"/world/notifications/{buyer}", headers=_auth(buyer)).json()["notifications"]
    assert any(n["type"] == "order_overdue" for n in notifs)
    # buyer can still cancel for a full refund
    bal_before = client.get(f"/world/profile/{buyer}", headers=_auth(buyer))
    r = client.post(f"/world/orders/{oid}/cancel", json={"agent_id": buyer}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"


def test_dispute_auto_refund_on_sla_timeout():
    prov, buyer = _uid("p"), _uid("b")
    for x in (prov, buyer):
        _make(x)
    sid = _service(prov)
    oid = _buy(buyer, sid)
    d = dail
    bal_after_buy = d.ledger.balances[buyer]
    r = client.post(f"/world/orders/{oid}/dispute", json={
        "agent_id": buyer, "reason": "bad"}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    order = d.world_agents.orders[oid]
    assert order["resolve_by"], "dispute should carry a 48h resolve_by"
    # time-travel past the SLA
    order["resolve_by"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    client.get("/world/orders", headers=_auth(buyer))  # sweep runs on read
    assert order["status"] == "refunded"
    assert order["resolution"] == "timeout_auto_refund"
    # buyer got the escrow back; the 1 DAIL dispute filing fee stays with the treasury
    assert d.ledger.balances[buyer] == bal_after_buy + order["amount"] - 1


def test_bounty_expiry_and_edit_and_release():
    poster, hunter = _uid("p"), _uid("h")
    for x in (poster, hunter):
        _make(x)
    d = dail
    bal_before = d.ledger.balances[poster]
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Expiring", "description": "d",
        "reward": 10, "expires_in_days": 1}, headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert r.json()["listing_expires_at"]  # v2: 7-day listing clock
    # edit while open
    r = client.patch(f"/world/bounties/{bid}", json={
        "agent_id": poster, "title": "Expiring (edited)"}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["title"] == "Expiring (edited)"
    # non-poster cannot edit
    r = client.patch(f"/world/bounties/{bid}", json={
        "agent_id": hunter, "title": "hijack"}, headers=_auth(hunter))
    assert r.status_code == 403, r.text
    # hunter claims, then withdraws
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200
    r = client.post(f"/world/bounties/{bid}/release", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200 and r.json()["status"] == "open"
    # time-travel past listing expiry -> escrow refunded, status expired
    b = d.world_agents.bounties[bid]
    b["listing_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    lst = client.get("/world/bounties").json()["bounties"]
    mine = [x for x in lst if x["id"] == bid][0]
    assert mine["status"] == "expired"
    assert d.ledger.balances[poster] == bal_before


def test_poster_filter_and_rating():
    prov, buyer = _uid("p"), _uid("b")
    for x in (prov, buyer):
        _make(x)
    r = client.post("/world/bounties", json={
        "agent_id": prov, "title": "Mine", "description": "d", "reward": 5},
        headers=_auth(prov))
    assert r.status_code == 201
    lst = client.get("/world/bounties", params={"poster": prov}).json()["bounties"]
    assert lst and all(b["poster_id"] == prov for b in lst)
    # rating on confirm aggregates on the service
    sid = _service(prov, price=10)
    oid = _buy(buyer, sid)
    d = dail
    client.post(f"/world/orders/{oid}/deliver", json={
        "agent_id": prov, "delivery": "done"}, headers=_auth(prov))
    r = client.post(f"/world/orders/{oid}/confirm", json={
        "agent_id": buyer, "rating": 5}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["rating"] == 5
    svcs = client.get("/world/services").json()["services"]
    mine = [s for s in svcs if s["id"] == sid][0]
    assert mine["rating_avg"] == 5.0 and mine["rating_count"] == 1
    # bad rating value rejected
    oid2 = _buy(buyer, sid)
    client.post(f"/world/orders/{oid2}/deliver", json={
        "agent_id": prov, "delivery": "done"}, headers=_auth(prov))
    r = client.post(f"/world/orders/{oid2}/confirm", json={
        "agent_id": buyer, "rating": 9}, headers=_auth(buyer))
    assert r.status_code == 422, r.text


def test_ledger_history_own_only():
    a, b = _uid("a"), _uid("b")
    for x in (a, b):
        _make(x)
    r = client.get(f"/world/ledger/{a}", headers=_auth(a))
    assert r.status_code == 200, r.text
    txs = r.json()["transactions"]
    assert txs and all(t["from_account"] == a or t["to_account"] == a for t in txs)
    # someone else's history is forbidden
    r = client.get(f"/world/ledger/{a}", headers=_auth(b))
    assert r.status_code == 403, r.text


def test_provider_cannot_dispute_before_delivery():
    # A provider disputing pre-delivery is pure grief: it freezes the buyer's
    # escrow and kills their instant-cancel path. Rejected outright.
    prov, buyer = _uid("p"), _uid("b")
    for x in (prov, buyer):
        _make(x)
    sid = _service(prov)
    oid = _buy(buyer, sid)
    r = client.post(f"/world/orders/{oid}/dispute", json={
        "agent_id": prov, "reason": "grief"}, headers=_auth(prov))
    assert r.status_code == 400, r.text
    # buyer can still dispute pre-delivery (their money is at stake)
    r = client.post(f"/world/orders/{oid}/dispute", json={
        "agent_id": buyer, "reason": "changed mind"}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
