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
    assert _bal(p, pk) == 100 and _bal(b, bk) == 69  # frozen: nobody paid; 1 dispute fee
    # Resolve is admin-only.
    assert client.post(f"/world/orders/{oid}/resolve",
                       json={"winner": "buyer"}).status_code == 403
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "buyer"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert _bal(b, bk) == 99  # full escrow refund minus 1 dispute fee


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
    assert _bal(b, bk) == 69  # 30 escrowed + 1 dispute fee


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
                    json={"agent_id": aid, "amount": 50, "provider": "test", "idempotency_key": "rh-safe-1"},
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


# --- Round 2 fixes: order cancel route + provider-side referral --------------

def _order_pending(provider_key, buyer_key, provider_id, buyer_id, price):
    """Buy a service; the order stays awaiting_delivery (no deliver yet)."""
    r = client.post("/world/services",
                    json={"provider_id": provider_id, "name": "svc",
                          "description": "d", "price": price},
                    headers=_auth(provider_key))
    svc = r.json()["id"]
    r = client.post("/world/services/purchase",
                    json={"buyer_id": buyer_id, "service_id": svc},
                    headers=_auth(buyer_key))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def test_order_cancel_refunds_buyer():
    p, b = _uid("cx"), _uid("cx")
    pk, bk = _make(p, ip="10.40.0.1"), _make(b, ip="10.40.0.2")
    oid = _order_pending(pk, bk, p, b, 30)
    assert _bal(b, bk) == 70  # 30 held in escrow
    r = client.post(f"/world/orders/{oid}/cancel",
                    json={"agent_id": b}, headers=_auth(bk))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"
    assert _bal(b, bk) == 100  # full escrow refund


def test_order_cancel_after_delivery_fails():
    p, b = _uid("cx"), _uid("cx")
    pk, bk = _make(p, ip="10.40.1.1"), _make(b, ip="10.40.1.2")
    oid = _order_open(pk, bk, p, b, 30)  # already delivered
    r = client.post(f"/world/orders/{oid}/cancel",
                    json={"agent_id": b}, headers=_auth(bk))
    assert r.status_code == 400, r.text
    assert _bal(b, bk) == 70  # escrow untouched


def test_order_cancel_by_non_buyer_fails():
    p, b = _uid("cx"), _uid("cx")
    pk, bk = _make(p, ip="10.40.2.1"), _make(b, ip="10.40.2.2")
    oid = _order_pending(pk, bk, p, b, 30)
    r = client.post(f"/world/orders/{oid}/cancel",
                    json={"agent_id": p}, headers=_auth(pk))
    assert r.status_code == 403, r.text
    assert _bal(b, bk) == 70  # escrow untouched


def test_referral_pays_provider_side():
    main = _uid("rp")
    main_k = _make(main, ip="10.41.0.1")
    prov = _uid("rp")
    prov_k = _make(prov, ip="10.41.0.2", referred_by=main)
    buyer = _uid("rp")
    buyer_k = _make(buyer, ip="10.41.0.3")
    _trade(prov_k, buyer_k, prov, buyer, 20)  # referred agent SELLS
    assert _bal(main, main_k) == 100 + 10  # provider-side referral paid


def test_referral_held_provider_side_circular():
    main = _uid("rp")
    main_k = _make(main, ip="10.42.0.1")
    prov = _uid("rp")
    prov_k = _make(prov, ip="10.42.0.2", referred_by=main)
    _trade(prov_k, main_k, prov, main, 20)  # referrer buys from referred provider
    held = [h for h in dail.world_agents.held_referrals() if h["agent_id"] == prov]
    assert len(held) == 1 and "referrer" in held[0]["reason"]
    assert _bal(main, main_k) == 80  # paid 20 as buyer; no +10 reward


# --- Round 3 fixes: referral anti-farming gate -------------------------------

def test_referral_dust_trade_earns_nothing_then_pays_on_real_trade():
    r = _uid("r3")
    rk = _make(r, ip="10.50.0.1")
    a = _uid("r3")
    ak = _make(a, ip="10.50.0.2", referred_by=r)
    seller = _uid("r3")
    sk = _make(seller, ip="10.50.0.3")
    # Dust trade: 2 DAIL wash — must NOT mint the reward, and must NOT hold
    # (a genuine agent's small first trade must not poison the referral).
    _trade(sk, ak, seller, a, 2)
    assert _bal(r, rk) == 100
    assert not [h for h in dail.world_agents.held_referrals() if h["agent_id"] == a]
    # Real trade: 15 DAIL — the pending referral now pays.
    _trade(sk, ak, seller, a, 15)
    assert _bal(r, rk) == 110


def test_referral_banned_referrer_held_not_paid():
    r = _uid("r3")
    rk = _make(r, ip="10.51.0.1")
    a = _uid("r3")
    ak = _make(a, ip="10.51.0.2", referred_by=r)
    seller = _uid("r3")
    sk = _make(seller, ip="10.51.0.3")
    dail.ban_agent(r, "round-3 probe")
    rbal_before = dail.world_agents.ledger.balances.get(r, 0)
    _trade(sk, ak, seller, a, 20)
    held = [h for h in dail.world_agents.held_referrals() if h["agent_id"] == a]
    assert len(held) == 1 and "banned" in held[0]["reason"]
    # Nothing minted into the dead account.
    assert dail.world_agents.ledger.balances.get(r, 0) == rbal_before


# --- Round 4 fixes: name reservation, bounty reject, dispute fee -----------

def test_staff_names_reserved_at_registration():
    r = client.post("/agents", json={"id": _uid("rn"), "name": "dail_host"},
                    headers={"X-Forwarded-For": "10.70.0.1"})
    assert r.status_code == 409, r.text
    r = client.post("/agents", json={"id": _uid("rn"), "name": "DAIL_MANAGER"},
                    headers={"X-Forwarded-For": "10.70.0.2"})
    assert r.status_code == 409, r.text
    r = client.post("/agents", json={"id": _uid("rn"), "name": "honest trader"},
                    headers={"X-Forwarded-For": "10.70.0.3"})
    assert r.status_code == 200, r.text


def test_staff_names_reserved_on_rename():
    aid = _uid("rn")
    key = _make(aid, ip="10.70.0.4")
    r = client.post("/social/identity",
                    json={"agent_id": aid, "name": "dail_inspector"},
                    headers=_auth(key))
    assert r.status_code == 400, r.text
    r = client.post("/social/identity",
                    json={"agent_id": aid, "name": "legit new name"},
                    headers=_auth(key))
    assert r.status_code == 200, r.text


def test_bounty_reject_reopens_and_blocks_griefer():
    p = _uid("rb"); pk = _make(p, ip="10.71.0.1")
    g = _uid("rb"); gk = _make(g, ip="10.71.0.2")
    h = _uid("rb"); hk = _make(h, ip="10.71.0.3")
    r = client.post("/world/bounties",
                    json={"agent_id": p, "title": "t", "description": "d", "reward": 25},
                    headers=_auth(pk))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    # Griefer claims with junk.
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": g, "submission": "junk"}, headers=_auth(gk))
    assert r.status_code == 200, r.text
    # Poster cannot cancel a claimed bounty, but CAN reject.
    r = client.post(f"/world/bounties/{bid}/cancel",
                    json={"agent_id": p}, headers=_auth(pk))
    assert r.status_code == 400, r.text
    r = client.post(f"/world/bounties/{bid}/reject",
                    json={"agent_id": p}, headers=_auth(pk))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    # Griefer cannot reclaim; a fresh hunter can.
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": g, "submission": "junk2"}, headers=_auth(gk))
    assert r.status_code == 400, r.text
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": h, "submission": "real work"}, headers=_auth(hk))
    assert r.status_code == 200, r.text
    # Poster accepts the good claim; escrow releases.
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": p}, headers=_auth(pk))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert _bal(h, hk) == 100 + 23  # 25 - 2 fee (10%)


def test_dispute_costs_fee():
    p, b = _uid("rf"), _uid("rf")
    pk, bk = _make(p, ip="10.72.0.1"), _make(b, ip="10.72.0.2")
    oid = _order_open(pk, bk, p, b, 30)
    assert _bal(b, bk) == 70
    r = client.post(f"/world/orders/{oid}/dispute",
                    json={"agent_id": b, "reason": "x"}, headers=_auth(bk))
    assert r.status_code == 200, r.text
    assert _bal(b, bk) == 69  # 30 escrowed + 1 dispute fee
    # Provider disputing also pays.
    p2, b2 = _uid("rf"), _uid("rf")
    pk2, bk2 = _make(p2, ip="10.72.0.3"), _make(b2, ip="10.72.0.4")
    oid2 = _order_open(pk2, bk2, p2, b2, 30)
    r = client.post(f"/world/orders/{oid2}/dispute",
                    json={"agent_id": p2, "reason": "y"}, headers=_auth(pk2))
    assert r.status_code == 200, r.text
    assert _bal(p2, pk2) == 99  # 1 dispute fee
