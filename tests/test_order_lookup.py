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
    return f"{prefix}_ol{_seq[0]:04d}"


def _make(aid, balance=100):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": balance})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _admin():
    return {"X-DAIL-Admin-Key": "test-admin-key"}


def _order():
    """One awaiting-delivery escrow order; returns (seller, buyer, oid)."""
    s, b = _uid("s"), _uid("b")
    _make(s)
    _make(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "Brief", "description": "d", "price": 10},
        headers=_auth(s)).json()
    r = client.post("/world/services/purchase",
                    json={"buyer_id": b, "service_id": svc["id"]},
                    headers=_auth(b))
    assert r.status_code == 200, r.text
    return s, b, r.json()["order_id"]


def test_buyer_can_get_own_order():
    s, b, oid = _order()
    r = client.get(f"/world/orders/{oid}", headers=_auth(b))
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == oid
    assert r.json()["status"] == "awaiting_delivery"


def test_provider_can_get_own_order():
    s, b, oid = _order()
    r = client.get(f"/world/orders/{oid}", headers=_auth(s))
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == oid


def test_third_party_gets_404_not_403():
    s, b, oid = _order()
    t = _uid("t")
    _make(t)
    r = client.get(f"/world/orders/{oid}", headers=_auth(t))
    assert r.status_code == 404, r.text


def test_missing_order_gets_404():
    s, b, oid = _order()
    r = client.get("/world/orders/ord_999999", headers=_auth(b))
    assert r.status_code == 404, r.text


def test_admin_can_get_any_order():
    s, b, oid = _order()
    r = client.get(f"/world/orders/{oid}", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == oid


def test_unauthenticated_get_is_rejected():
    s, b, oid = _order()
    r = client.get(f"/world/orders/{oid}")
    assert r.status_code == 401, r.text
