"""Subscriptions (deep-dive Phase G): consent, due-period charging into a
normal escrow order, pause on insufficient funds, cancel."""
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

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_sub{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _setup(price=10):
    prov, buyer = _uid("p"), _uid("b")
    _make(prov)
    _make(buyer)
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": price},
        headers=_auth(prov))
    sid = r.json()["id"]
    r = client.post("/world/subscriptions", json={
        "agent_id": buyer, "service_id": sid, "interval_days": 7},
        headers=_auth(buyer))
    assert r.status_code == 201, r.text
    return prov, buyer, r.json()["id"]


def test_charge_creates_escrow_order_and_advances_due():
    prov, buyer, sid = _setup()
    bb = _bal(buyer)
    r = client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["periods_paid"] == 1
    assert r.json()["order"]["status"] == "awaiting_delivery"
    assert r.json()["order"]["amount"] == 10
    assert _bal(buyer) == bb - 10
    # not due again yet
    r = client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 400
    # time-travel past the interval: second period charges
    dail.world_agents.subscriptions[sid]["next_due_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": prov},
                    headers=_auth(prov))
    assert r.status_code == 200
    assert r.json()["subscription"]["periods_paid"] == 2


def test_pause_on_insufficient_funds_and_cancel():
    prov, buyer, sid = _setup(price=90)
    # buyer test grant is 100: first charge ok, second (after time-travel) fails
    client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": buyer},
                headers=_auth(buyer))
    dail.world_agents.subscriptions[sid]["next_due_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 400
    subs = client.get(f"/world/subscriptions?agent_id={buyer}",
                      headers=_auth(buyer)).json()["subscriptions"]
    assert subs[0]["status"] == "paused"
    # provider cannot cancel; buyer can
    r = client.post(f"/world/subscriptions/{sid}/cancel", json={"agent_id": prov},
                    headers=_auth(prov))
    assert r.status_code == 403
    r = client.post(f"/world/subscriptions/{sid}/cancel", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
