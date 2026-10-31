"""Trade-gated probation + feed rotation (deep-dive Phase J remainder):
newcomer shelf and the 20-listing probation cap for unproven providers."""
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
    return f"{prefix}_jr{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _service(prov, price=5):
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": price},
        headers=_auth(prov))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_newcomer_shelf_and_trade_exit():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov)
    _make(buyer)
    sid = _service(prov)
    shelf = client.get("/world/services/new").json()["services"]
    assert any(s["id"] == sid for s in shelf)
    # complete a trade: provider leaves the shelf
    _seq[0] += 1
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": f"jrkey{_seq[0]:04d}"}, headers=_auth(buyer))
    oid = r.json()["order_id"]
    client.post(f"/world/orders/{oid}/deliver", json={
        "agent_id": prov, "delivery": "done"}, headers=_auth(prov))
    client.post(f"/world/orders/{oid}/confirm", json={
        "agent_id": buyer}, headers=_auth(buyer))
    shelf = client.get("/world/services/new").json()["services"]
    assert not any(s["id"] == sid for s in shelf)


def test_probation_cap_lifts_after_first_trade():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov)
    _make(buyer)
    first = None
    for i in range(20):
        sid = _service(prov)
        if first is None:
            first = sid
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": 5},
        headers=_auth(prov))
    assert r.status_code == 400
    # first trade lifts the cap
    _seq[0] += 1
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": first,
        "idempotency_key": f"jrkey{_seq[0]:04d}"}, headers=_auth(buyer))
    oid = r.json()["order_id"]
    client.post(f"/world/orders/{oid}/deliver", json={
        "agent_id": prov, "delivery": "done"}, headers=_auth(prov))
    client.post(f"/world/orders/{oid}/confirm", json={
        "agent_id": buyer}, headers=_auth(buyer))
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "s", "description": "d", "price": 5},
        headers=_auth(prov))
    assert r.status_code == 200, r.text
