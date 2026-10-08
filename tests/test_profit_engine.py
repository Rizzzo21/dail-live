import sys
import os
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
    return f"{prefix}_pe{_seq[0]}"


def _make(aid, balance=100, referred_by=""):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": balance, "referred_by": referred_by})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _admin():
    return {"X-DAIL-Admin-Key": "test-admin-key"}


def _bal(aid):
    r = client.get(f"/ledger/{aid}", headers=_auth(aid))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def _treasury():
    return client.get("/treasury").json()


def test_trade_fee_flows_to_treasury():
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    t0 = _treasury()["balance"]
    r = client.post("/world/trades", json={
        "seller_id": s, "buyer_id": b, "amount": 100,
        "item": "widget", "idempotency_key": f"k-{s}"}, headers=_auth(b))
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["fee"] == 10 and t["seller_net"] == 90
    assert _bal(b) == 0           # buyer paid 100
    assert _bal(s) == 190         # seller: 100 + 90
    assert _treasury()["balance"] == t0 + 10


def test_minimum_fee_floor_on_micro_trade():
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    t0 = _treasury()["balance"]
    r = client.post("/world/trades", json={
        "seller_id": s, "buyer_id": b, "amount": 5,
        "item": "micro", "idempotency_key": f"micro-{s}"}, headers=_auth(b))
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["fee"] == 1 and t["seller_net"] == 4  # 10% of 5 truncates to 0 -> floor 1
    assert _treasury()["balance"] == t0 + 1


def test_trade_replay_still_idempotent_with_fee():
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    body = {"seller_id": s, "buyer_id": b, "amount": 100,
            "item": "w", "idempotency_key": f"rk-{s}"}
    assert client.post("/world/trades", json=body, headers=_auth(b)).status_code == 200
    assert client.post("/world/trades", json=body, headers=_auth(b)).status_code == 200
    assert _bal(b) == 0 and _bal(s) == 190  # charged exactly once


def test_escrow_order_lifecycle_with_fee():
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "Brief", "description": "d", "price": 100},
        headers=_auth(s)).json()
    t0 = _treasury()["balance"]
    # purchase -> escrow hold
    r = client.post("/world/services/purchase",
                    json={"buyer_id": b, "service_id": svc["id"]},
                    headers=_auth(b))
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["status"] == "awaiting_delivery"
    oid = o["order_id"]
    assert _bal(b) == 0 and _bal(s) == 100  # held, not yet paid
    # deliver
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": s, "delivery": "done"}, headers=_auth(s))
    assert r.status_code == 200 and r.json()["status"] == "delivered"
    assert _bal(s) == 100  # still held
    # confirm -> release with fee
    r = client.post(f"/world/orders/{oid}/confirm",
                    json={"agent_id": b}, headers=_auth(b))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert r.json()["fee"] == 10
    assert _bal(s) == 190 and _bal(b) == 0
    assert _treasury()["balance"] == t0 + 10


def test_order_dispute_and_buyer_refund():
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "X", "description": "d", "price": 40},
        headers=_auth(s)).json()
    o = client.post("/world/services/purchase",
                    json={"buyer_id": b, "service_id": svc["id"]},
                    headers=_auth(b)).json()
    oid = o["order_id"]
    r = client.post(f"/world/orders/{oid}/dispute",
                    json={"agent_id": b, "reason": "never delivered"},
                    headers=_auth(b))
    assert r.status_code == 200 and r.json()["status"] == "disputed"
    # confirm while disputed is blocked
    r = client.post(f"/world/orders/{oid}/confirm",
                    json={"agent_id": b}, headers=_auth(b))
    assert r.status_code == 400
    # resolve needs the admin key in the header (never the body)
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"admin_key": "test-admin-key", "winner": "buyer"})
    assert r.status_code == 403
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "buyer"}, headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"
    assert _bal(b) == 99 and _bal(s) == 100  # full refund minus 1 DAIL dispute fee


def test_referral_reward_on_first_trade():
    inviter, new, buyer = _uid("inv"), _uid("new"), _uid("buy")
    _make(inviter)                       # 100
    _make(new, referred_by=inviter)      # 100
    _make(buyer)                         # 100, unrelated third party
    # Backdate buyer so the burst guard doesn't fire: genuinely distinct agent.
    # NOTE: dail is imported at module level (not inside this test), because
    # test_payment_safety.py evicts dail.* from sys.modules mid-suite — a
    # function-level re-import would bind a fresh, disconnected Dail object.
    from datetime import datetime, timedelta, timezone
    dail.world_agents.agent_created[buyer] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    # trade 1: new sells 20 to buyer (a real third party, not the referrer).
    # fee = max(1, 20*1000//10000) = 2.
    # buyer: 100 - 20 = 80; new: 100 + 18 = 118; inviter: 100 + 10 reward = 110.
    r = client.post("/world/trades", json={
        "seller_id": new, "buyer_id": buyer, "amount": 20,
        "item": "y", "idempotency_key": f"ref-{new}"}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert _bal(buyer) == 80, _bal(buyer)
    assert _bal(new) == 118, _bal(new)
    assert _bal(inviter) == 110, _bal(inviter)
    # trade 2: reward must not pay twice. buyer: 80 - 20 = 60.
    r = client.post("/world/trades", json={
        "seller_id": new, "buyer_id": buyer, "amount": 20,
        "item": "y2", "idempotency_key": f"ref2-{new}"}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert _bal(buyer) == 60, _bal(buyer)
    assert _bal(inviter) == 110, _bal(inviter)
    # reward recorded: inviter got a referral_reward notification, exactly once
    notifs = client.get(f"/world/notifications/{inviter}",
                        headers=_auth(inviter)).json()["notifications"]
    rewards = [n for n in notifs if n.get("type") == "referral_reward"]
    assert len(rewards) == 1 and rewards[0]["amount"] == 10


def test_treasury_report_shape():
    t = _treasury()
    assert t["treasury"] == "dail:treasury"
    assert t["currency"] == "DAIL"
    assert t["fee_bps"] == 1000
    assert isinstance(t["events"], list)


def test_quickstart_served():
    r = client.get("/quickstart")
    assert r.status_code == 200
    assert "escrow" in r.text.lower()


def test_one_dail_order_settles_fee_takes_all():
    # 1-DAIL orders: fee floor (1) takes the whole amount. Must settle cleanly
    # (no LedgerError on a zero transfer) and must not poison the sweep.
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "Micro", "description": "d", "price": 1},
        headers=_auth(s)).json()
    t0 = _treasury()["balance"]
    r = client.post("/world/services/purchase",
                    json={"buyer_id": b, "service_id": svc["id"]},
                    headers=_auth(b))
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": s, "delivery": "done"}, headers=_auth(s))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/orders/{oid}/confirm",
                    json={"agent_id": b}, headers=_auth(b))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert _bal(s) == 100  # provider nets 0; fee took it all
    assert _treasury()["balance"] == t0 + 1


def test_self_purchase_rejected():
    s = _uid("s")
    _make(s)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "Mine", "description": "d", "price": 20},
        headers=_auth(s)).json()
    r = client.post("/world/services/purchase",
                    json={"buyer_id": s, "service_id": svc["id"]},
                    headers=_auth(s))
    assert r.status_code == 400, r.text
    assert "own service" in r.text


def test_trade_replay_returns_same_record():
    # Record-level idempotency: same key -> same trade_NNNN, no duplicate.
    s, b = _uid("s"), _uid("b")
    _make(s); _make(b)
    body = {"seller_id": s, "buyer_id": b, "amount": 30,
            "item": "w", "idempotency_key": "dup-trade-1"}
    r1 = client.post("/world/trades", json=body, headers=_auth(b))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/world/trades", json=body, headers=_auth(b))
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] == r2.json()["id"]
    # and the buyer was only charged once
    assert _bal(b) == 70
