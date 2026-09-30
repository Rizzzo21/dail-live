"""Rogue-hardening regression tests (2026-09-29).

Covers the five fixes from the red-team exercise:
1. Registration faucet guard: per-IP rate limit on POST /agents.
2. Referral wash-trade guard: circular/shared-IP/burst trades HOLD the
   10-DAIL auto-reward for manual admin review instead of paying out.
3. Escrow/dispute process: deliver -> dispute -> admin resolve, plus the
   7-day auto-release. (Process already existed; these tests pin it.)
4. Staff prompt-injection scanning: enforced at the bouncer-cron layer
   (no backend change; covered operationally).
5. Closed loop: the safe is DAIL-denominated with no USD rail (real_funds=False).
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"  # 10%, floor 1 DAIL
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
_seq = [0]
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_rh{_seq[0]}"


def _make(aid, ip=None, referred_by=None):
    headers = {}
    if ip:
        headers["X-Forwarded-For"] = ip
    body = {"id": aid, "name": aid}
    if referred_by:
        body["referred_by"] = referred_by
    r = client.post("/agents", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _bal(aid, key):
    return client.get(f"/ledger/{aid}", headers=_auth(key)).json()["balance"]


def _trade(provider_key, buyer_key, provider_id, buyer_id, price):
    """List a service, buy it, deliver, confirm. Returns the order."""
    r = client.post("/world/services",
                    json={"provider_id": provider_id, "name": "svc",
                          "description": "d", "price": price},
                    headers=_auth(provider_key))
    assert r.status_code == 200, r.text
    svc = r.json()["id"]
    r = client.post("/world/services/purchase",
                    json={"buyer_id": buyer_id, "service_id": svc},
                    headers=_auth(buyer_key))
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": provider_id, "delivery": "done"},
                    headers=_auth(provider_key))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/orders/{oid}/confirm",
                    json={"agent_id": buyer_id},
                    headers=_auth(buyer_key))
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. Registration faucet guard -------------------------------------------

def test_registration_rate_limit(monkeypatch):
    monkeypatch.setenv("DAIL_REG_LIMIT", "2")
    ip = "10.9.9.1"
    a, b = _uid("rl"), _uid("rl")
    assert client.post("/agents", json={"id": a, "name": a},
                       headers={"X-Forwarded-For": ip}).status_code == 200
    assert client.post("/agents", json={"id": b, "name": b},
                       headers={"X-Forwarded-For": ip}).status_code == 200
    c = _uid("rl")
    r = client.post("/agents", json={"id": c, "name": c},
                    headers={"X-Forwarded-For": ip})
    assert r.status_code == 429, r.text
    # A different address is unaffected.
    d = _uid("rl")
    assert client.post("/agents", json={"id": d, "name": d},
                       headers={"X-Forwarded-For": "10.9.9.2"}).status_code == 200


def test_registration_records_ip_and_timestamp():
    aid, ip = _uid("rip"), "10.9.9.3"
    _make(aid, ip=ip)
    assert dail.world_agents.agent_ips[aid] == ip
    assert dail.world_agents.agent_created[aid]  # ISO timestamp present


# --- 2. Referral wash-trade guard --------------------------------------------

def test_referral_held_when_counterparty_is_referrer():
    main = _uid("ref")
    main_k = _make(main, ip="10.10.0.1")
    alt = _uid("ref")
    alt_k = _make(alt, ip="10.10.0.2", referred_by=main)
    # Alt "trades" with its own referrer: the classic circular wash.
    order = _trade(main_k, alt_k, main, alt, 20)
    assert order["status"] == "completed"
    # Reward is HELD, not paid.
    assert _bal(main, main_k) == 100 + 18  # 20 - 10% fee; no +10 reward
    held = [h for h in dail.world_agents.held_referrals()
            if h["agent_id"] == alt]
    assert len(held) == 1
    assert "referrer" in held[0]["reason"]


def test_referral_held_on_shared_registration_ip():
    main = _uid("ref")
    main_k = _make(main, ip="10.10.1.1")
    alt = _uid("ref")
    alt_k = _make(alt, ip="10.10.1.9", referred_by=main)
    carol = _uid("ref")
    carol_k = _make(carol, ip="10.10.1.9")  # same IP as alt: alt farm
    order = _trade(carol_k, alt_k, carol, alt, 20)
    assert order["status"] == "completed"
    assert _bal(main, main_k) == 100  # no reward paid
    held = [h for h in dail.world_agents.held_referrals()
            if h["agent_id"] == alt]
    assert len(held) == 1
    assert "IP" in held[0]["reason"]


def test_referral_pays_when_trade_is_clean():
    main = _uid("ref")
    main_k = _make(main, ip="10.10.2.1")
    alt = _uid("ref")
    alt_k = _make(alt, ip="10.10.2.2", referred_by=main)
    carol = _uid("ref")
    carol_k = _make(carol, ip="10.10.2.3")
    # Backdate carol so the burst signal doesn't fire: genuinely distinct agent.
    dail.world_agents.agent_created[carol] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    order = _trade(carol_k, alt_k, carol, alt, 20)
    assert order["status"] == "completed"
    assert _bal(main, main_k) == 100 + 10  # referral reward paid
    assert not [h for h in dail.world_agents.held_referrals()
                if h["agent_id"] == alt]


def test_admin_referral_review_flow():
    main = _uid("ref")
    main_k = _make(main, ip="10.10.3.1")
    alt = _uid("ref")
    alt_k = _make(alt, ip="10.10.3.2", referred_by=main)
    _trade(main_k, alt_k, main, alt, 20)  # circular -> held
    # Admin gate: no key -> 403.
    assert client.get("/admin/referrals/held").status_code == 403
    r = client.get("/admin/referrals/held", headers=ADMIN)
    assert r.status_code == 200
    assert any(h["agent_id"] == alt for h in r.json()["held"])
    # Deny: never pays.
    r = client.post("/admin/referrals/release",
                    json={"agent_id": alt, "approve": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["paid"] is False
    assert _bal(main, main_k) == 118
    assert not [h for h in dail.world_agents.held_referrals()
                if h["agent_id"] == alt]
    # Releasing a non-held referral -> 404.
    r = client.post("/admin/referrals/release",
                    json={"agent_id": alt, "approve": True}, headers=ADMIN)
    assert r.status_code == 404


def test_admin_referral_approve_pays():
    main = _uid("ref")
    main_k = _make(main, ip="10.10.4.1")
    alt = _uid("ref")
    alt_k = _make(alt, ip="10.10.4.2", referred_by=main)
    _trade(main_k, alt_k, main, alt, 20)  # circular -> held
    r = client.post("/admin/referrals/release",
                    json={"agent_id": alt, "approve": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["paid"] is True
    assert _bal(main, main_k) == 118 + 10


# --- 3. Escrow / dispute process ----------------------------------------------

def _order_open(provider_key, buyer_key, provider_id, buyer_id, price):
    r = client.post("/world/services",
                    json={"provider_id": provider_id, "name": "svc",
                          "description": "d", "price": price},
                    headers=_auth(provider_key))
    svc = r.json()["id"]
    r = client.post("/world/services/purchase",
                    json={"buyer_id": buyer_id, "service_id": svc},
                    headers=_auth(buyer_key))
    oid = r.json()["order_id"]
    r = client.post(f"/world/orders/{oid}/deliver",
                    json={"agent_id": provider_id, "delivery": "deliverable"},
                    headers=_auth(provider_key))
    assert r.status_code == 200, r.text
    return oid


def test_dispute_freezes_and_admin_resolves_to_buyer():
    p, b = _uid("dsp"), _uid("dsb")
    pk, bk = _make(p, ip="10.20.0.1"), _make(b, ip="10.20.0.2")
    oid = _order_open(pk, bk, p, b, 30)
    assert _bal(b, bk) == 70  # 30 held in escrow
    r = client.post(f"/world/orders/{oid}/dispute",
                    json={"agent_id": b, "reason": "not as described"},
                    headers=_auth(bk))
    assert r.status_code == 200 and r.json()["status"] == "disputed"
    assert _bal(p, pk) == 100 and _bal(b, bk) == 70  # frozen: nobody paid
    # Resolve is admin-only.
    assert client.post(f"/world/orders/{oid}/resolve",
                       json={"winner": "buyer"}).status_code == 403
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "buyer"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert _bal(b, bk) == 100  # full refund, no fee


def test_dispute_resolved_to_provider_takes_fee():
    p, b = _uid("dsp"), _uid("dsb")
    pk, bk = _make(p, ip="10.20.1.1"), _make(b, ip="10.20.1.2")
    oid = _order_open(pk, bk, p, b, 30)
    client.post(f"/world/orders/{oid}/dispute",
                json={"agent_id": b, "reason": "changed mind"},
                headers=_auth(bk))
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "provider"}, headers=ADMIN)
    assert r.status_code == 200
    assert _bal(p, pk) == 100 + 27  # 30 - 10% fee
    assert _bal(b, bk) == 70


def test_escrow_auto_releases_after_seven_days():
    p, b = _uid("dsp"), _uid("dsb")
    pk, bk = _make(p, ip="10.20.2.1"), _make(b, ip="10.20.2.2")
    oid = _order_open(pk, bk, p, b, 20)
    # Backdate delivery 8 days; the next order read sweeps it.
    dail.world_agents.orders[oid]["delivered_at"] = (
        datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    r = client.get("/world/orders", params={"agent_id": p}, headers=_auth(pk))
    assert r.status_code == 200
    got = [o for o in r.json()["orders"] if o["order_id"] == oid][0]
    assert got["status"] == "completed" and got.get("auto_released") is True
    assert _bal(p, pk) == 100 + 18


# --- 5. Closed loop: no USD rail -------------------------------------------------

def test_safe_is_dail_denominated_no_cash_out():
    aid = _uid("safe")
    key = _make(aid, ip="10.30.0.1")
    r = client.get("/safe", headers=_auth(key))
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["real_funds"] is False
    assert info["currency"] == "DAIL"
    # Fund the safe, configure a withdrawal key, withdraw to a destination.
    r = client.post("/safe/receive",
                    json={"amount": 50, "provider": "test", "idempotency_key": "rh-safe-1"},
                    headers=_auth(key))
    assert r.status_code == 200, r.text
    r = client.post("/safe/keys/withdrawal", headers=ADMIN)
    assert r.status_code == 200, r.text
    wdkey = r.json()["withdrawal_key"]
    assert wdkey.startswith("dail_wd_")
    r = client.post("/safe/withdraw",
                    json={"amount": 50, "destination": "somewhere",
                          "idempotency_key": "rh-wd-1"},
                    headers={"X-DAIL-Withdrawal-Key": wdkey, **_auth(key)})
    assert r.status_code == 200, r.text
    # The "withdrawal" is still DAIL inside the ledger: closed loop holds.
    tx = r.json()
    assert tx["to_account"] == "withdrawal:somewhere"
