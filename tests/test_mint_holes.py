"""Mint-hole fixes (2026-10-07): no unauthenticated or unbacked DAIL creation.

- POST /deposits (mock gateway) is disabled in live mode.
- POST /safe/receive moves the agent's own DAIL; it never mints.
- Referral rewards are drawn from the vault, not minted ad hoc.
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
    return f"{prefix}_mint{_seq[0]}"


def _make(aid, **kw):
    r = client.post("/agents", json={"id": aid, "name": aid, **kw})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _make_ip(aid, ip, **kw):
    r = client.post("/agents", json={"id": aid, "name": aid, **kw},
                    headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _supply():
    return sum(v for k, v in dail.ledger.balances.items()
               if not k.startswith("escrow:"))


def test_mock_deposit_disabled_in_live_mode(monkeypatch):
    a = _uid("a")
    _make(a)
    before = _supply()
    monkeypatch.setenv("DAIL_REAL_PAYMENTS", "true")
    r = client.post("/deposits", json={
        "agent_id": a, "amount": 1000000, "provider": "mock",
        "idempotency_key": _uid("k")}, headers=_auth(a))
    assert r.status_code == 403, r.text
    assert _supply() == before  # nothing minted


def test_safe_receive_moves_funds_no_mint():
    a = _uid("a")
    _make(a)
    before = _supply()
    safe_before = dail.ledger.balances.get("DAIL_SAFE", 0)
    bal_before = dail.ledger.balances[a]
    r = client.post("/safe/receive", json={
        "agent_id": a, "amount": 20, "provider": "test",
        "idempotency_key": _uid("k")}, headers=_auth(a))
    assert r.status_code == 200, r.text
    assert dail.ledger.balances["DAIL_SAFE"] == safe_before + 20
    assert dail.ledger.balances[a] == bal_before - 20
    assert _supply() == before  # supply unchanged: a pure transfer
    # another agent's key cannot fund from a's balance
    b = _uid("b")
    _make(b)
    r = client.post("/safe/receive", json={
        "agent_id": a, "amount": 5, "provider": "test",
        "idempotency_key": _uid("k")}, headers=_auth(b))
    assert r.status_code == 403, r.text


def test_referral_reward_comes_from_vault():
    from datetime import datetime, timezone, timedelta
    referrer, referred, seller = _uid("r"), _uid("e"), _uid("s")
    _make_ip(referrer, "10.60.0.1")
    _make_ip(referred, "10.60.0.2", referred_by=referrer)
    _make_ip(seller, "10.60.0.3")
    # backdate the seller so the burst signal doesn't fire
    dail.world_agents.agent_created[seller] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    vault_before = dail.ledger.balances.get("dail:vault", 0)
    ref_before = dail.ledger.balances[referrer]
    before = _supply()
    # qualifying trade with a third party (not the referrer — wash guard)
    r = client.post("/world/services", json={
        "provider_id": seller, "name": "s", "description": "d",
        "price": 50}, headers=_auth(seller))
    sid = r.json()["id"]
    r = client.post("/world/services/purchase", json={
        "buyer_id": referred, "service_id": sid,
        "idempotency_key": _uid("k")}, headers=_auth(referred))
    oid = r.json()["order_id"]
    client.post(f"/world/orders/{oid}/deliver",
                json={"agent_id": seller, "payload": "x"},
                headers=_auth(seller))
    client.post(f"/world/orders/{oid}/confirm",
                json={"agent_id": referred}, headers=_auth(referred))
    ref = dail.world_agents.referrals.get(referred)
    assert ref and ref.get("paid") is True
    assert dail.ledger.balances[referrer] == ref_before + 10
    assert dail.ledger.balances.get("dail:vault", 0) == vault_before - 10
    assert _supply() == before  # vault debit, not a mint
