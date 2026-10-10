"""Security patch 2026-10-10 (4th claim sweep, 15:45 PDT): two real bugs found by
kaminari-research and paid on merit.

- bnty_0068: sender-spoofing on /runtime/messages — the old check only required
  the caller to be *one* of the two endpoints, so a caller listed as the
  recipient could spoof any other agent's sender_id. Fix: sender is derived
  from the caller's key for non-admin callers.
- bnty_0083: confirm/dispute race (TOCTOU) — HTTP 500 crash + phantom 1-DAIL
  dispute fee (ord_0025/ord_0023, tx_000616/000617). Fix: the status check,
  the fee charge, and the status flip are one atomic step under
  _mutation_lock; the loser of the race gets a clean ValueError and pays no
  fee, and escrow is never released on a disputed order.
"""
import os
import sys
import threading
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
    return f"{prefix}_sp{_seq[0]:04d}"


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


def _deliver(provider, oid):
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": provider, "delivery": "done"},
                    headers=_auth(provider))
    assert r.status_code == 200, r.text


def test_runtime_message_sender_derived_from_caller_key():
    # bnty_0068: a caller listed as the recipient cannot spoof someone else's
    # sender_id. The message must land "from" the caller.
    spoofer = _uid("spoofer")
    victim = _uid("victim")
    _make(spoofer)
    _make(victim)
    r = client.post("/runtime/messages",
                    json={"sender_id": victim, "recipient_id": spoofer,
                          "message": "you owe me money"},
                    headers=_auth(spoofer))
    assert r.status_code == 200, r.text
    pkt = r.json()
    assert pkt["from"] == spoofer, f"spoofed sender leaked through: {pkt}"
    assert pkt["to"] == spoofer
    # The real victim's inbox must not contain a message claiming to be from
    # them that they never sent.
    r = client.get(f"/runtime/messages/{victim}", headers=_auth(victim))
    assert r.status_code == 200, r.text
    mine = r.json()["messages"]
    assert not any(m["from"] == victim for m in mine)


def test_confirm_and_dispute_race_has_exactly_one_winner():
    # bnty_0083: hammer confirm vs dispute on a delivered order from many
    # threads. Exactly one transition must win; the loser pays nothing and
    # the escrow is never double-released.
    provider = _uid("provider")
    _make(provider)
    sid = _service(provider, price=5)
    w = dail.world_agents
    wins = {"confirmed": 0, "disputed": 0}
    errors = []

    def one_round(i):
        # Fresh buyer per round (each registration carries a 10-DAIL grant:
        # 5 for the purchase + at most 1 for a dispute fee).
        buyer = _uid(f"buyer{i}")
        _make(buyer)
        oid = _buy(buyer, sid)
        _deliver(provider, oid)
        results = []

        def do_confirm():
            try:
                w.confirm_order(buyer, oid)
                results.append("confirmed")
            except (ValueError, PermissionError, KeyError) as e:
                results.append(f"confirm-err:{e}")

        def do_dispute():
            try:
                w.dispute_order(buyer, oid, "late")
                results.append("disputed")
            except (ValueError, PermissionError, KeyError) as e:
                results.append(f"dispute-err:{e}")

        threads = [threading.Thread(target=do_confirm) for _ in range(8)] + \
                  [threading.Thread(target=do_dispute) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("confirmed") + results.count("disputed") == 1, \
            f"more than one winner on {oid}: {results}"
        if results.count("confirmed") == 1:
            wins["confirmed"] += 1
            o = w.orders[oid]
            assert o["status"] == "completed"
            # no dispute fee charged on the confirmed path
            fees = [t for t in w.ledger_for(buyer)["transactions"]
                    if t["idempotency_key"] == f"dispute-fee:{oid}"]
            assert fees == [], f"phantom dispute fee on {oid}: {fees}"
        else:
            wins["disputed"] += 1
            o = w.orders[oid]
            assert o["status"] == "disputed"
            # exactly one dispute fee
            fees = [t for t in w.ledger_for(buyer)["transactions"]
                    if t["idempotency_key"] == f"dispute-fee:{oid}"]
            assert len(fees) == 1, f"fee count wrong on {oid}: {fees}"
            # escrow must still be locked, not released to the provider
            releases = [t for t in w.ledger_for(provider)["transactions"]
                        if t["idempotency_key"] == f"escrow-release:{oid}"]
            assert releases == [], f"escrow released on disputed {oid}"
        assert not errors

    for i in range(5):
        one_round(i)
    assert wins["confirmed"] + wins["disputed"] == 5
