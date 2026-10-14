"""Webhook dedup + out-of-order handling — end-to-end tests (bnty_0163).

Drives the real FastAPI app through the real HTTP route
``POST /payments/webhook`` with a fake ``stripe`` module and a throwaway SQLite
database, so every ordering scenario is exercised without touching Stripe.

The three scenarios the brief names, plus the two the brief implies:

  1. duplicate    — the same event delivered 3x credits exactly once.
  2. delayed      — an older event arriving after a newer one resolves correctly.
  3. out-of-order — refund-before-credit: a ``charge.refunded`` that arrives
                    BEFORE the ``checkout.session.completed`` that pays the
                    session must still end with the DAIL reversed (the refund is
                    deferred and applied when the credit lands), never leaving
                    the agent holding DAIL for a refunded charge.
  4. convergence  — cumulative partial-refund events delivered out of order
                    (100c, 300c, 200c) converge to exactly the 300c reversed.
  5. event-id dedup — the same Stripe ``event.id`` delivered twice is a no-op
                    regardless of type.

FAIL->PASS: on the unpatched service every test in the OUT-OF-ORDER and EVENT-ID
groups fails (there is no ``dail_webhook_events`` table, no deferred-refund
path, and a refund that arrives first is dropped with
``{"reconciled": False, "reason": "unknown_payment_intent"}``), while the DAIL
stays credited. On the patched service they pass. Run the same file against a
pristine checkout to see the FAIL half.
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

# --------------------------------------------------------------- fake stripe
fake_stripe = types.ModuleType("stripe")
fake_stripe.api_key = None

_sessions = {}
_idem_sessions = {}
REFUND_CALLS = []
_refunds = {}
_refund_idem = {}


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def get(self, k, default=None):
        return getattr(self, k, default)


class _SessionAPI:
    counter = 0

    @classmethod
    def create(cls, **kwargs):
        idem = kwargs.get("idempotency_key")
        if idem and idem in _idem_sessions:
            return _idem_sessions[idem]
        cls.counter += 1
        sid = f"cs_test_{cls.counter:04d}"
        s = _Obj(id=sid, url=f"https://checkout.stripe.com/pay/{sid}",
                 payment_intent=f"pi_test_{cls.counter:04d}")
        _sessions[sid] = s
        if idem:
            _idem_sessions[idem] = s
        return s

    @classmethod
    def retrieve(cls, sid):
        return _sessions.get(sid)


class _RefundAPI:
    counter = 0

    @classmethod
    def create(cls, payment_intent=None, amount=None, idempotency_key=None,
               metadata=None):
        REFUND_CALLS.append({"payment_intent": payment_intent, "amount": amount,
                             "idempotency_key": idempotency_key,
                             "metadata": metadata or {}})
        if idempotency_key and idempotency_key in _refund_idem:
            return _refund_idem[idempotency_key]
        cls.counter += 1
        r = _Obj(id=f"re_test_{cls.counter:04d}", amount=amount,
                 payment_intent=payment_intent, status="succeeded")
        _refunds[r.id] = r
        if idempotency_key:
            _refund_idem[idempotency_key] = r
        return r


class _WebhookAPI:
    @staticmethod
    def construct_event(payload, signature, secret):
        if signature != "valid-signature":
            raise Exception("bad signature")
        return json.loads(payload.decode("utf-8"))


fake_stripe.checkout = types.SimpleNamespace(Session=_SessionAPI)
fake_stripe.Refund = _RefundAPI
fake_stripe.Webhook = _WebhookAPI
sys.modules["stripe"] = fake_stripe

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = "/tmp/dail_webhook_ordering_test.db"
_ENV = {
    "DAIL_REAL_PAYMENTS": "true",
    "STRIPE_SECRET_KEY": "sk_test_fake",
    "STRIPE_WEBHOOK_SECRET": "whsec_fake",
    "DATABASE_URL": f"sqlite:///{DB}",
    "DAIL_PER_USD": "1",
    "DAIL_ADMIN_KEY": "test-admin-key",
}


@pytest.fixture(scope="module")
def client():
    def _evict_dail():
        for mod in [m for m in list(sys.modules)
                    if m == "dail" or m.startswith("dail.")]:
            del sys.modules[mod]

    if os.path.exists(DB):
        os.remove(DB)
    old_env = {k: os.environ.get(k) for k in _ENV}
    os.environ.update(_ENV)
    # Re-bind OUR fake stripe right before importing dail: another test module
    # (e.g. test_stripe_refund.py) may have replaced sys.modules["stripe"] at
    # import time, and production_payments binds `stripe` at module load.
    sys.modules["stripe"] = fake_stripe
    _evict_dail()
    try:
        from fastapi.testclient import TestClient
        import dail.api as api_module
        yield TestClient(api_module.app)
    finally:
        _evict_dail()
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------- helpers
def _agent(client, aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _checkout(client, aid, key, usd_cents=500):
    body = {"agent_id": aid, "usd_cents": usd_cents,
            "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
            "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
    r = client.post("/payments/checkout", json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    return r.json()


def _post_event(client, etype, obj, event_id=None, signature="valid-signature"):
    """Deliver one Stripe event exactly as Stripe would (JSON body + signature)."""
    event = {"type": etype, "data": {"object": obj}}
    if event_id:
        event["id"] = event_id
    r = client.post("/payments/webhook",
                    content=json.dumps(event).encode(),
                    headers={"stripe-signature": signature})
    return r


def _completed(client, sid, pi, event_id=None):
    return _post_event(client, "checkout.session.completed",
                       {"id": sid, "payment_intent": pi}, event_id=event_id)


def _refunded(client, pi, amount_refunded, event_id=None):
    return _post_event(client, "charge.refunded",
                       {"payment_intent": pi, "amount_refunded": amount_refunded},
                       event_id=event_id)


def _balance(client, aid, key):
    r = client.get(f"/ledger/{aid}", headers=_auth(key))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


# ------------------------------------------------------ 1. duplicate events
def test_duplicate_event_credits_once(client):
    """The same session-completed event delivered 3x credits DAIL exactly once."""
    key = _agent(client, "wh_dup")
    start = _balance(client, "wh_dup", key)
    co = _checkout(client, "wh_dup", key, 500)
    sid, pi = co["session_id"], f"pi_dup_{co['session_id']}"
    r1 = _completed(client, sid, pi, event_id="evt_dup_1")
    assert r1.status_code == 200 and r1.json()["duplicate"] is False, r1.text
    assert _balance(client, "wh_dup", key) == start + 5
    # Two replays: one by event id, one with no event id (session-keyed dedup).
    r2 = _completed(client, sid, pi, event_id="evt_dup_1")
    r3 = _completed(client, sid, pi)
    assert r2.status_code == 200 and r3.status_code == 200, (r2.text, r3.text)
    assert _balance(client, "wh_dup", key) == start + 5


def test_same_event_id_deduped_across_types(client):
    """A replayed event id is a no-op even for a different event type."""
    key = _agent(client, "wh_evid")
    co = _checkout(client, "wh_evid", key, 500)
    sid, pi = co["session_id"], f"pi_evid_{co['session_id']}"
    _completed(client, sid, pi, event_id="evt_same_1")
    bal = _balance(client, "wh_evid", key)
    # Same event id, now claiming a refund: must be ignored (already processed).
    r = _refunded(client, pi, 500, event_id="evt_same_1")
    assert r.status_code == 200, r.text
    assert _balance(client, "wh_evid", key) == bal


# --------------------------------------------------- 2. out-of-order: refund first
def test_refund_before_credit_is_not_lost(client):
    """charge.refunded arriving BEFORE the credit must not leave DAIL credited.

    Sequence: refund event first (session not yet paid), then the credit.
    End state must be: DAIL credited AND the refunded DAIL reversed — net 0
    above the starting balance.
    """
    key = _agent(client, "wh_ooo")
    start = _balance(client, "wh_ooo", key)
    co = _checkout(client, "wh_ooo", key, 500)
    sid, pi = co["session_id"], f"pi_ooo_{co['session_id']}"
    # Refund first — Stripe can deliver charge.refunded before we see completed.
    r_ref = _refunded(client, pi, 500, event_id="evt_ooo_ref")
    assert r_ref.status_code == 200, r_ref.text
    # Now the credit lands.
    r_pay = _completed(client, sid, pi, event_id="evt_ooo_pay")
    assert r_pay.status_code == 200, r_pay.text
    # The refund must have been applied once the credit landed: net zero.
    assert _balance(client, "wh_ooo", key) == start, (
        f"refund-before-credit was lost: balance {_balance(client, 'wh_ooo', key)}"
        f" != start {start}")


def test_refund_before_credit_replay_is_idempotent(client):
    """Replaying the pre-credit refund after the credit is applied is a no-op."""
    key = _agent(client, "wh_ooo2")
    start = _balance(client, "wh_ooo2", key)
    co = _checkout(client, "wh_ooo2", key, 400)
    sid, pi = co["session_id"], f"pi_ooo2_{co['session_id']}"
    _refunded(client, pi, 400, event_id="evt_ooo2_ref")
    _completed(client, sid, pi, event_id="evt_ooo2_pay")
    assert _balance(client, "wh_ooo2", key) == start
    # Replay the original refund event (no event id) — still no double debit.
    r = _refunded(client, pi, 400)
    assert r.status_code == 200, r.text
    assert _balance(client, "wh_ooo2", key) == start


# ------------------------------------------------------ 3. delayed / older event
def test_delayed_expired_after_paid_is_ignored(client):
    """An older checkout.session.expired arriving after the credit is a no-op."""
    key = _agent(client, "wh_delay")
    co = _checkout(client, "wh_delay", key, 500)
    sid, pi = co["session_id"], f"pi_delay_{co['session_id']}"
    _completed(client, sid, pi, event_id="evt_delay_pay")
    bal = _balance(client, "wh_delay", key)
    r = _post_event(client, "checkout.session.expired", {"id": sid},
                    event_id="evt_delay_exp")
    assert r.status_code == 200, r.text
    # The delayed expiry must not claw back a paid session.
    assert _balance(client, "wh_delay", key) == bal


def test_delayed_completed_after_expired_still_credits(client):
    """A delayed completion arriving after expiry still credits (async pay)."""
    key = _agent(client, "wh_delay2")
    start = _balance(client, "wh_delay2", key)
    co = _checkout(client, "wh_delay2", key, 500)
    sid, pi = co["session_id"], f"pi_delay2_{co['session_id']}"
    _post_event(client, "checkout.session.expired", {"id": sid},
                event_id="evt_delay2_exp")
    r = _completed(client, sid, pi, event_id="evt_delay2_pay")
    assert r.status_code == 200, r.text
    assert _balance(client, "wh_delay2", key) == start + 5


# ---------------------------------------------------- 4. out-of-order partials
def test_out_of_order_partial_refunds_converge(client):
    """Cumulative partials delivered out of order converge to the right total."""
    key = _agent(client, "wh_conv")
    start = _balance(client, "wh_conv", key)
    co = _checkout(client, "wh_conv", key, 500)
    sid, pi = co["session_id"], f"pi_conv_{co['session_id']}"
    _completed(client, sid, pi, event_id="evt_conv_pay")
    assert _balance(client, "wh_conv", key) == start + 5
    # Cumulative amount_refunded: 100c, then 300c, then a stale 200c re-delivery.
    _refunded(client, pi, 100, event_id="evt_conv_r1")
    _refunded(client, pi, 300, event_id="evt_conv_r2")
    _refunded(client, pi, 200, event_id="evt_conv_r3")   # stale, out of order
    # 300c of 500c reversed => 3 DAIL reversed, 2 DAIL remain.
    assert _balance(client, "wh_conv", key) == start + 2


def test_full_refund_after_partials_converges(client):
    """A final full-refund event converges to exactly the credited DAIL."""
    key = _agent(client, "wh_conv2")
    start = _balance(client, "wh_conv2", key)
    co = _checkout(client, "wh_conv2", key, 300)
    sid, pi = co["session_id"], f"pi_conv2_{co['session_id']}"
    _completed(client, sid, pi, event_id="evt_conv2_pay")
    _refunded(client, pi, 100, event_id="evt_conv2_r1")
    _refunded(client, pi, 300, event_id="evt_conv2_r2")
    assert _balance(client, "wh_conv2", key) == start
