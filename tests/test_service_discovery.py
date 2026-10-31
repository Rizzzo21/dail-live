"""Trade-ranked discovery (deep-dive build, Phase A): GET /world/services
gains opt-in sort and min_trades filtering over existing counters."""
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
    return f"{prefix}_sd{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _service(provider, price=5):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": 72}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _complete_trade(buyer, sid):
    r = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": _uid("k")}, headers=_auth(buyer))
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    # provider is the service owner; deliver then buyer confirms
    svc = [s for s in client.get("/world/services").json()["services"] if s["id"] == sid][0]
    prov = svc["provider_id"]
    r = client.post(f"/world/orders/{oid}/deliver", json={
        "agent_id": prov, "delivery": "done"}, headers=_auth(prov))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/orders/{oid}/confirm", json={
        "agent_id": buyer}, headers=_auth(buyer))
    assert r.status_code == 200, r.text


def _setup():
    prov_a, prov_b, prov_c, buyer = _uid("pa"), _uid("pb"), _uid("pc"), _uid("buy")
    for x in (prov_a, prov_b, prov_c, buyer):
        _make(x)
    sa = _service(prov_a, price=30)
    sb = _service(prov_b, price=10)
    sc = _service(prov_c, price=20)
    _complete_trade(buyer, sa)
    _complete_trade(buyer, sa)
    _complete_trade(buyer, sc)
    return sa, sb, sc


def test_sort_trades_and_min_trades():
    sa, sb, sc = _setup()
    rows = client.get("/world/services?sort=trades").json()["services"]
    ids = [r["id"] for r in rows]
    assert ids.index(sa) < ids.index(sc) < ids.index(sb)
    assert {r["id"]: r["orders_completed"] for r in rows}[sa] == 2
    assert {r["id"]: r["orders_completed"] for r in rows}[sc] == 1
    rows = client.get("/world/services?sort=trades&min_trades=1").json()["services"]
    ids = [r["id"] for r in rows]
    assert sa in ids and sc in ids and sb not in ids


def test_sort_price_and_bad_params():
    sa, sb, sc = _setup()
    rows = client.get("/world/services?sort=price_asc").json()["services"]
    mine = [r for r in rows if r["id"] in (sa, sb, sc)]
    assert [r["id"] for r in mine] == [sb, sc, sa]
    assert client.get("/world/services?sort=bogus").status_code == 400
    assert client.get("/world/services?min_trades=-1").status_code == 400


def test_default_order_unchanged_and_counters_normalised():
    sa, sb, sc = _setup()
    rows = client.get("/world/services").json()["services"]
    mine = [r for r in rows if r["id"] in (sa, sb, sc)]
    assert [r["id"] for r in mine] == [sa, sb, sc]
    assert all("orders_completed" in r and "orders_disputed" in r for r in rows)
