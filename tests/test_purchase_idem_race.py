"""Purchase idempotency race fix 2026-10-14 (bnty_0173, found by innerozma-probe).

The hole: purchase_service checked the idempotency map, created the order,
and moved escrow WITHOUT holding _mutation_lock, and the DB idem_claim ran
AFTER the escrow transfer. N concurrent purchases with the same
(buyer_id, idempotency_key) each missed the map, each created an order, and
each moved escrow -- while every client saw a single order_id (the DB claim
loser just returned the winner's id). 8x escrow for 1 logical purchase.

Fix: the whole idem-check -> reserve -> escrow-hold -> order-create sequence
runs under _mutation_lock, and the DB key is reserved BEFORE the ledger
transfer (released if the transfer/persist fails, so a retry never hits a
poisoned key with no backing order).
"""
import os
import sys
import threading
import time
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
    return f"{prefix}_pr{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _service(provider, price=1):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": 72}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _balance(aid):
    r = client.get(f"/ledger/{aid}", headers=_auth(aid))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def test_concurrent_same_key_creates_exactly_one_order(monkeypatch):
    buyer, provider = _uid("b"), _uid("p")
    _make(buyer)
    _make(provider)
    sid = _service(provider, price=1)
    key = _uid("idem")
    before = _balance(buyer)

    # Widen the race window deterministically: the sleep sits inside the
    # check-then-create section, so all 8 threads are guaranteed to be
    # inside it simultaneously on the unfixed code.
    real_transfer = dail.world_agents.ledger.transfer

    def _slow(*a, **k):
        time.sleep(0.05)
        return real_transfer(*a, **k)

    monkeypatch.setattr(dail.world_agents.ledger, "transfer", _slow)

    results, errors = [], []
    gate = threading.Barrier(8)

    def _fire():
        try:
            gate.wait(timeout=10)  # all 8 threads enter the race together
            r = client.post("/world/services/purchase", json={
                "buyer_id": buyer, "service_id": sid,
                "idempotency_key": key}, headers=_auth(buyer))
            results.append((r.status_code, r.json().get("order_id")))
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    threads = [threading.Thread(target=_fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(results) == 8
    assert all(code == 200 for code, _ in results), results
    oids = {oid for _, oid in results}
    assert len(oids) == 1, f"forked orders: {results}"
    # Exactly one order exists for this buyer/service, one escrow hold.
    mine = [o for o in dail.world_agents.orders.values()
            if o["buyer_id"] == buyer and o["service_id"] == sid]
    assert len(mine) == 1, f"expected 1 order, got {len(mine)}"
    assert _balance(buyer) == before - 1, "escrow moved more than once"


def test_sequential_replay_returns_same_order_single_hold():
    buyer, provider = _uid("b"), _uid("p")
    _make(buyer)
    _make(provider)
    sid = _service(provider, price=2)
    key = _uid("idem")
    before = _balance(buyer)
    r1 = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": key}, headers=_auth(buyer))
    r2 = client.post("/world/services/purchase", json={
        "buyer_id": buyer, "service_id": sid,
        "idempotency_key": key}, headers=_auth(buyer))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert _balance(buyer) == before - 2


def test_failed_transfer_releases_reservation_retry_succeeds():
    buyer, provider = _uid("b"), _uid("p")
    _make(buyer)
    _make(provider)
    sid = _service(provider, price=3)
    key = _uid("idem")
    before = _balance(buyer)

    ledger = dail.world_agents.ledger
    real_transfer = ledger.transfer
    calls = [0]

    def _flaky(*a, **k):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("simulated transfer failure")
        return real_transfer(*a, **k)

    ledger.transfer = _flaky
    try:
        try:
            client.post("/world/services/purchase", json={
                "buyer_id": buyer, "service_id": sid,
                "idempotency_key": key}, headers=_auth(buyer))
            raise AssertionError("expected the simulated failure to surface")
        except RuntimeError as e:
            assert "simulated transfer failure" in str(e)
        r2 = client.post("/world/services/purchase", json={
            "buyer_id": buyer, "service_id": sid,
            "idempotency_key": key}, headers=_auth(buyer))
        assert r2.status_code == 200, r2.text
    finally:
        ledger.transfer = real_transfer
    mine = [o for o in dail.world_agents.orders.values()
            if o["buyer_id"] == buyer and o["service_id"] == sid]
    assert len(mine) == 1, f"expected 1 order after retry, got {len(mine)}"
    assert _balance(buyer) == before - 3
