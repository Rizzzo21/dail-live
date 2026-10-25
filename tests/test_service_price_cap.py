"""New-listing service price cap (2026-10-24): POST /world/services
rejects price > 10000 DAIL. New listings only — pre-cap listings,
including absurd ones, keep reading back unchanged.
"""
import os
import sys
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
    return f"{prefix}_cap{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _post_service(provider, price):
    return client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": 72}, headers=_auth(provider))


def test_cap_boundary_accepted():
    prov = _uid("p")
    _make(prov)
    r = _post_service(prov, 10000)
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 10000


def test_cap_rejects_above():
    prov = _uid("p")
    _make(prov)
    r = _post_service(prov, 10001)
    assert r.status_code == 422, r.text
    assert "price_cap_exceeded" in r.text
    mine = [s for s in client.get("/world/services").json()["services"]
            if s["provider_id"] == prov]
    assert mine == [], "rejected listing must not be stored"


def test_existing_absurd_listing_untouched():
    # Simulates a pre-cap listing: created through the store path, not the
    # capped POST route, exactly like the absurd prices already live.
    prov = _uid("p")
    _make(prov)
    svc = dail.world_agents.create_service(
        prov, "absurd", "pre-cap listing", 10 ** 20)
    assert svc["price"] == 10 ** 20
    back = [s for s in client.get("/world/services").json()["services"]
            if s["id"] == svc["id"]]
    assert len(back) == 1
    assert back[0]["price"] == 10 ** 20
    assert back[0]["provider_id"] == prov
