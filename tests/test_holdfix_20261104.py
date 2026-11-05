"""Hold fix (2026-11-04, Tommy's ruling): review holds last at most 4 hours;
purchase responses carry delivery_hours; orders accept buyer requirements;
staff get a verification-scoped order read while agent reads stay caller-scoped.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail
from dail.models import Agent
from dail.service import review_hold_active, review_hold_expires_at

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_hf{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _manager_key():
    try:
        dail.create_agent(
            Agent(id="dail_manager", name="Manager", goal="g", balance=0))
    except ValueError:
        pass  # already exists in this test session
    return {"Authorization": f"Bearer {dail.keystore.issue('dail_manager')}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _submitted_bounty(poster, hunter, reward=20, days_since_submit=8):
    """Poster posts, hunter claims with a submission, submission backdated
    past the 7-day review SLA (mirrors tests/test_bounties.py)."""
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": reward},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim",
                json={"agent_id": hunter,
                      "submission": "real work, delivered in full, honest numbers, every figure checked against the primary source and reproduced twice"},
                headers=_auth(hunter))
    wa = dail.world_agents
    wa.bounties[bid]["submitted_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days_since_submit)).isoformat()
    return wa, bid


def _service(provider, price=20, hours=72):
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": price, "delivery_hours": hours}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _buy(buyer, sid, extra=None):
    body = {"buyer_id": buyer, "service_id": sid,
            "idempotency_key": f"key-hf-{_uid('k')}"}
    if extra:
        body.update(extra)
    r = client.post("/world/services/purchase", json=body, headers=_auth(buyer))
    return r


# --- 1. Hold lifetime: fixed timestamps, no sleeps ---------------------------

def test_hold_active_within_4h_and_inactive_after():
    held = {"review_hold": True, "review_hold_at": "2026-11-04T12:00:00+00:00"}
    assert review_hold_active(
        held, datetime(2026, 11, 4, 15, 59, tzinfo=timezone.utc)) is True
    # Exactly at review_hold_at + 4h the hold is no longer active
    # (active only while now < review_hold_at + 4h).
    assert review_hold_active(
        held, datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)) is False
    assert review_hold_active(
        held, datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)) is False
    assert review_hold_expires_at(held) == "2026-11-04T16:00:00+00:00"
    assert review_hold_active({"review_hold": False,
                               "review_hold_at": "2026-11-04T12:00:00+00:00"},
                              datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)) is False
    assert review_hold_expires_at({"review_hold": False,
                                   "review_hold_at": None}) is None


def test_hold_without_timestamp_stays_active():
    # Legacy hold records predate review_hold_at; expiry cannot be proven,
    # so the hold must not silently unpause the backstop.
    assert review_hold_active({"review_hold": True, "review_hold_at": None},
                              datetime.now(timezone.utc)) is True


def test_expired_hold_no_longer_pauses_auto_accept():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    wa, bid = _submitted_bounty(poster, hunter)
    r = client.post(f"/world/bounties/{bid}/review/hold",
                    json={"agent_id": poster, "reason": "gated"}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    # Age the hold past 4h (fixed timestamp, no sleep).
    held_at = datetime.now(timezone.utc) - timedelta(hours=5)
    wa.bounties[bid]["review_hold_at"] = held_at.isoformat()
    # Serialization exposes the computed expiry.
    listed = [b for b in client.get("/world/bounties").json()["bounties"]
              if b["id"] == bid][0]
    assert listed["review_hold_expires_at"] == (held_at + timedelta(hours=4)).isoformat()
    wa.sweep_bounties()
    assert wa.bounties[bid]["status"] == "completed"
    assert _bal(hunter) == 120  # 100 grant + 20, first-accepted fee waived


def test_fresh_hold_still_pauses_auto_accept():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    wa, bid = _submitted_bounty(poster, hunter)
    r = client.post(f"/world/bounties/{bid}/review/hold",
                    json={"agent_id": poster, "reason": "gated"}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    wa.sweep_bounties()
    assert wa.bounties[bid]["status"] == "submitted"
    assert _bal(hunter) == 100


# --- 2. Purchase response carries delivery_hours ------------------------------

def test_purchase_response_carries_delivery_hours():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov); _make(buyer)
    sid = _service(prov, hours=48)
    r = _buy(buyer, sid)
    assert r.status_code == 200, r.text
    assert r.json()["delivery_hours"] == 48


# --- 3. Order requirements ----------------------------------------------------

def test_order_requirements_roundtrip_visible_to_both_parties():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov); _make(buyer)
    sid = _service(prov)
    req = "Use Python 3.12, no network calls, deliver a single .py file."
    r = _buy(buyer, sid, extra={"requirements": req})
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    assert r.json()["requirements"] == req
    assert client.get(f"/world/orders/{oid}", headers=_auth(buyer)).json()["requirements"] == req
    assert client.get(f"/world/orders/{oid}", headers=_auth(prov)).json()["requirements"] == req


def test_order_without_requirements_is_none():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov); _make(buyer)
    sid = _service(prov)
    r = _buy(buyer, sid)
    assert r.status_code == 200, r.text
    assert r.json()["requirements"] is None


def test_order_requirements_over_length_is_422():
    prov, buyer = _uid("p"), _uid("b")
    _make(prov); _make(buyer)
    sid = _service(prov)
    r = _buy(buyer, sid, extra={"requirements": "x" * 2001})
    assert r.status_code == 422, r.text


# --- 4. Staff verification read vs caller-scoped agent read -------------------

def test_staff_order_read_non_party_and_agent_read_404s():
    prov, buyer, stranger = _uid("p"), _uid("b"), _uid("s")
    _make(prov); _make(buyer); _make(stranger)
    sid = _service(prov)
    oid = _buy(buyer, sid).json()["order_id"]
    # Staff (not a party) can read it for verification.
    r = client.get(f"/staff/orders/{oid}", headers=_manager_key())
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == oid
    # An ordinary non-party agent gets 404, same as a missing order.
    r = client.get(f"/world/orders/{oid}", headers=_auth(stranger))
    assert r.status_code == 404, r.text
    # A non-staff agent is forbidden from the staff endpoint entirely.
    r = client.get(f"/staff/orders/{oid}", headers=_auth(stranger))
    assert r.status_code == 403, r.text
    # Staff read of a missing order is 404, not a leak.
    r = client.get("/staff/orders/ord_999999", headers=_manager_key())
    assert r.status_code == 404, r.text
