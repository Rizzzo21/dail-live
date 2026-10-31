"""Versioned service cards (Phase H) and trade-concentration stats (Phase J)."""
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
    return f"{prefix}_vg{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_versioned_card_and_sample():
    prov = _uid("p")
    _make(prov)
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "v1", "description": "d", "price": 5,
        "sample_deliverable": "sample v1"}, headers=_auth(prov))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["version"] == 1
    assert r.json()["sample_deliverable"] == "sample v1"
    r = client.patch(f"/world/services/{sid}", json={
        "provider_id": prov, "name": "v2", "price": 7}, headers=_auth(prov))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert len(r.json()["versions"]) == 1
    assert r.json()["versions"][0]["name"] == "v1"
    assert r.json()["versions"][0]["price"] == 5


def test_trade_concentration_stats():
    # One provider completes 2 trades, another completes 0: Gini > 0.
    prov_a, prov_b, buyer = _uid("pa"), _uid("pb"), _uid("buy")
    for x in (prov_a, prov_b, buyer):
        _make(x)
    r = client.post("/world/services", json={
        "provider_id": prov_a, "name": "s", "description": "d", "price": 5},
        headers=_auth(prov_a))
    sa = r.json()["id"]
    client.post("/world/services", json={
        "provider_id": prov_b, "name": "s", "description": "d", "price": 5},
        headers=_auth(prov_b))
    for _ in range(2):
        _seq[0] += 1
        r = client.post("/world/services/purchase", json={
            "buyer_id": buyer, "service_id": sa,
            "idempotency_key": f"vgkey{_seq[0]:04d}"}, headers=_auth(buyer))
        assert r.status_code == 200, r.text
        oid = r.json()["order_id"]
        r = client.post(f"/world/orders/{oid}/deliver", json={
            "agent_id": prov_a, "delivery": "done"}, headers=_auth(prov_a))
        assert r.status_code == 200, r.text
        r = client.post(f"/world/orders/{oid}/confirm", json={
            "agent_id": buyer}, headers=_auth(buyer))
        assert r.status_code == 200, r.text
    stats = client.get("/world/stats").json()
    assert stats["trade_completed_orders"] >= 2
    assert stats["trade_volume_gini"] is not None
    assert 0 <= stats["trade_volume_gini"] <= 1
    assert stats["trade_share_outside_top10"] is not None
