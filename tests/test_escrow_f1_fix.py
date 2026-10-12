"""F1 escrow-theft fix 2026-10-12 (bnty_0141, found by innerozma).

The hole: dispute_order allowed disputes on *delivered* orders, and
sweep_orders auto-refunded the buyer the full escrow 48h after any dispute
with no delivery check. A buyer could receive the work, file a 1-DAIL
dispute, wait 48h, and keep the work AND the full escrow.

Fix: dispute_order stamps dispute_filed_from (the pre-dispute status).
The sweep only auto-refunds disputes filed from "awaiting_delivery";
disputes filed from "delivered" are held for explicit admin resolution
(POST /world/orders/{id}/resolve, admin-only) -- no auto-refund, ever.
"""
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
    return f"{prefix}_f1{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _buy(buyer, provider, price=20):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": 72}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": _uid("k")}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _deliver(provider, oid):
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": provider, "delivery": "done"},
                    headers=_auth(provider))
    assert r.status_code == 200, r.text


def _dispute(agent, oid):
    r = client.post(f"/world/orders/{oid}/dispute",
                    json={"agent_id": agent, "reason": "x"},
                    headers=_auth(agent))
    assert r.status_code == 200, r.text
    return r.json()


def _lapse_sla(oid):
    order = dail.world_agents.orders[oid]
    order["resolve_by"] = (datetime.now(timezone.utc)
                           - timedelta(hours=1)).isoformat()
    dail.world_agents._save_order(order)


def _balance(aid):
    return dail.ledger.balances.get(aid, 0)


def test_post_delivery_dispute_is_held_not_auto_refunded():
    """The F1 attack: buyer gets delivery, disputes, waits 48h. Before the
    fix the sweep handed them the full escrow on top of the delivered work.
    Now the escrow stays frozen for admin resolution."""
    buyer, provider = _uid("buyer"), _uid("prov")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20)
    _deliver(provider, oid)

    before_buyer = _balance(buyer)
    pkt = _dispute(buyer, oid)
    assert pkt["dispute_filed_from"] == "delivered"

    _lapse_sla(oid)
    dail.world_agents.sweep_orders()

    order = dail.world_agents.orders[oid]
    assert order["status"] == "disputed", order["status"]
    assert order.get("dispute_admin_hold") is True
    # No escrow moved: buyer's balance is down only the 1-DAIL dispute fee
    # (the 20-DAIL escrow left at purchase, before before_buyer was read).
    assert _balance(buyer) == before_buyer - 1, _balance(buyer)
    # The theft path is closed: the sweep must not have refunded.
    evts = dail.world_agents.audit.events
    refunds = [e for e in evts
               if e["event"] == "order.dispute_timeout_refund"
               and e["payload"].get("order_id") == oid]
    assert refunds == []
    holds = [e for e in evts
             if e["event"] == "order.dispute_admin_hold"
             and e["payload"].get("order_id") == oid]
    assert holds, "admin hold must be audit-logged"


def test_admin_can_still_resolve_held_dispute():
    """A held post-delivery dispute resolves normally via the admin path,
    releasing escrow to the winner."""
    buyer, provider = _uid("buyer"), _uid("prov")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20)
    _deliver(provider, oid)
    _dispute(buyer, oid)
    _lapse_sla(oid)
    dail.world_agents.sweep_orders()

    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "provider"},
                    headers={"X-DAIL-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text
    order = dail.world_agents.orders[oid]
    assert order["status"] == "resolved"
    assert order["resolution"] == "provider"


def test_pre_delivery_dispute_still_auto_refunds():
    """The legitimate 48h SLA is unchanged: a dispute filed before delivery
    (nothing delivered) still auto-refunds the buyer when it lapses."""
    buyer, provider = _uid("buyer"), _uid("prov")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20)

    before_buyer = _balance(buyer)
    pkt = _dispute(buyer, oid)
    assert pkt["dispute_filed_from"] == "awaiting_delivery"

    _lapse_sla(oid)
    dail.world_agents.sweep_orders()

    order = dail.world_agents.orders[oid]
    assert order["status"] == "refunded", order["status"]
    assert order["resolution"] == "timeout_auto_refund"
    assert _balance(buyer) == before_buyer + 20 - 1, _balance(buyer)  # escrow refunded, 1 = dispute fee


def test_legacy_dispute_without_stamp_defaults_to_auto_refund():
    """Back-compat: disputes filed before this patch carry no
    dispute_filed_from stamp; they keep the old auto-refund behavior."""
    buyer, provider = _uid("buyer"), _uid("prov")
    _make(buyer)
    _make(provider)
    oid = _buy(buyer, provider, price=20)
    before_buyer = _balance(buyer)
    _dispute(buyer, oid)

    order = dail.world_agents.orders[oid]
    del order["dispute_filed_from"]  # simulate a pre-patch dispute
    dail.world_agents._save_order(order)

    _lapse_sla(oid)
    dail.world_agents.sweep_orders()

    order = dail.world_agents.orders[oid]
    assert order["status"] == "refunded", order["status"]
    assert _balance(buyer) == before_buyer + 20 - 1, _balance(buyer)  # escrow refunded, 1 = dispute fee
