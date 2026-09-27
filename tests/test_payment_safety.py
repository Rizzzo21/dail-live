"""Safety tests for the production payment boundary.

Uses a fake `stripe` module and a throwaway SQLite database so webhook
ordering, idempotency, replay protection, expiry handling, rate limiting,
and signature verification are all verified without touching real Stripe.

The app is imported lazily inside a fixture (with this module's env vars),
so test collection order can never leak env state into other test modules.
"""
import sys
import types
import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------- fake stripe
# Installed at import time (before any dail.api import anywhere), but it is
# inert: it only takes effect for app instances built while it is present.
fake_stripe = types.ModuleType("stripe")
fake_stripe.api_key = None
_sessions = {}
_idem_sessions = {}


class _FakeSession:
    def __init__(self, sid, url):
        self.id = sid
        self.url = url


class _SessionAPI:
    counter = 0

    @classmethod
    def create(cls, **kwargs):
        idem = kwargs.get("idempotency_key")
        if idem and idem in _idem_sessions:
            return _idem_sessions[idem]
        cls.counter += 1
        sid = f"cs_test_{cls.counter:04d}"
        s = _FakeSession(sid, f"https://checkout.stripe.com/pay/{sid}")
        _sessions[sid] = s
        if idem:
            _idem_sessions[idem] = s
        return s


class _WebhookAPI:
    @staticmethod
    def construct_event(payload, signature, secret):
        if signature != "valid-signature":
            raise Exception("bad signature")
        return json.loads(payload.decode("utf-8"))


fake_stripe.checkout = types.SimpleNamespace(Session=_SessionAPI)
fake_stripe.Webhook = _WebhookAPI
sys.modules["stripe"] = fake_stripe

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = "/tmp/dail_payment_safety_test.db"
_SAFETY_ENV = {
    "DAIL_REAL_PAYMENTS": "true",
    # Live-prefixed fake key: the suite exercises the live-mode path
    # (announce, status flags) while the fake stripe module keeps every
    # Stripe call in-process. Test-mode behavior is covered by
    # test_mode_derived_from_key_prefix below.
    "STRIPE_SECRET_KEY": "sk_live_fake",
    "STRIPE_WEBHOOK_SECRET": "whsec_fake",
    "DATABASE_URL": f"sqlite:///{DB}",
    "DAIL_PER_USD": "1",
    "DAIL_ADMIN_KEY": "test-admin-key",
}


@pytest.fixture(scope="module")
def client():
    """Build a private app instance with the safety-test env vars."""
    def _evict_dail():
        # dail.production_payments captures `stripe` and env at import;
        # evict everything so the re-import below sees this module's env.
        for mod in [m for m in list(sys.modules) if m == "dail" or m.startswith("dail.")]:
            del sys.modules[mod]
    if os.path.exists(DB):
        os.remove(DB)
    old_env = {k: os.environ.get(k) for k in _SAFETY_ENV}
    os.environ.update(_SAFETY_ENV)
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


def _make_agent(client, aid):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": 0})
    assert r.status_code == 200, r.text


def _checkout(client, agent_id, usd_cents=500, idempotency_key=None):
    body = {"agent_id": agent_id, "usd_cents": usd_cents,
            "success_url": "https://example.com/s",
            "cancel_url": "https://example.com/c"}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    r = client.post("/payments/checkout", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _webhook(client, event_type, session_id, signature="valid-signature"):
    payload = json.dumps({"type": event_type, "data": {"object": {"id": session_id}}}).encode()
    return client.post("/payments/webhook", content=payload,
                       headers={"stripe-signature": signature})


def _balance(client, agent_id):
    return client.get(f"/ledger/{agent_id}").json()["balance"]


def test_webhook_credits_once_and_replay_is_safe(client):
    _make_agent(client, "buyer_replay")
    co = _checkout(client, "buyer_replay", 500)
    assert _balance(client, "buyer_replay") == 0
    r = _webhook(client, "checkout.session.completed", co["session_id"])
    assert r.status_code == 200, r.text
    assert r.json()["duplicate"] is False
    assert r.json()["transaction_id"]
    assert _balance(client, "buyer_replay") == 5
    # Replay the same webhook: credited exactly once.
    r2 = _webhook(client, "checkout.session.completed", co["session_id"])
    assert r2.json()["duplicate"] is True
    assert _balance(client, "buyer_replay") == 5


def test_async_payment_succeeded_also_credits(client):
    _make_agent(client, "buyer_async")
    co = _checkout(client, "buyer_async", 300)
    r = _webhook(client, "checkout.session.async_payment_succeeded", co["session_id"])
    assert r.status_code == 200
    assert _balance(client, "buyer_async") == 3


def test_bad_signature_rejected(client):
    _make_agent(client, "buyer_sig")
    co = _checkout(client, "buyer_sig", 500)
    r = _webhook(client, "checkout.session.completed", co["session_id"], signature="bogus")
    assert r.status_code == 400
    assert _balance(client, "buyer_sig") == 0


def test_unknown_session_rejected(client):
    r = _webhook(client, "checkout.session.completed", "cs_test_nonexistent")
    assert r.status_code == 400


def test_expired_session_marked(client):
    _make_agent(client, "buyer_exp")
    co = _checkout(client, "buyer_exp", 500)
    r = _webhook(client, "checkout.session.expired", co["session_id"])
    assert r.status_code == 200
    assert r.json()["event"] == "checkout.session.expired"
    assert _balance(client, "buyer_exp") == 0


def test_idempotent_checkout_creation(client):
    _make_agent(client, "buyer_idem")
    a = _checkout(client, "buyer_idem", 700, idempotency_key="key-abc")
    b = _checkout(client, "buyer_idem", 700, idempotency_key="key-abc")
    assert a["session_id"] == b["session_id"]
    assert a["checkout_url"] == b["checkout_url"]


def test_pending_cap_rate_limits(client):
    _make_agent(client, "buyer_cap")
    for _ in range(25):
        _checkout(client, "buyer_cap", 100)
    r = client.post("/payments/checkout", json={"agent_id": "buyer_cap", "usd_cents": 100,
                                                "success_url": "https://example.com/s",
                                                "cancel_url": "https://example.com/c"})
    assert r.status_code == 429, r.text


def test_announce_broadcasts_rail_to_agents(client):
    _make_agent(client, "buyer_announce")
    r = client.post("/payments/announce",
                    headers={"X-DAIL-Admin-Key": "wrong-key"})
    assert r.status_code == 403
    r = client.post("/payments/announce",
                    headers={"X-DAIL-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text
    assert r.json()["agents_notified"] >= 1
    notes = client.get("/world/notifications/buyer_announce").json()["notifications"]
    assert any(n.get("type") == "payment_rail_live" for n in notes)


def test_topup_credit_notifies_agent(client):
    _make_agent(client, "buyer_noted")
    co = _checkout(client, "buyer_noted", 200)
    _webhook(client, "checkout.session.completed", co["session_id"])
    notes = client.get("/world/notifications/buyer_noted").json()["notifications"]
    assert any(n.get("type") == "topup_credited" and n.get("amount_dail") == 2
               for n in notes)


def test_new_agent_is_told_about_rail(client):
    _make_agent(client, "buyer_newbie")
    notes = client.get("/world/notifications/buyer_newbie").json()["notifications"]
    assert any(n.get("type") == "payment_rail_live" for n in notes)


def test_mode_derived_from_key_prefix():
    """Mode reporting must be honest: test keys never report live."""
    from dail.production_payments import ProductionPayments

    class D:
        agents = {}

    saved = {k: os.environ.get(k) for k in ("STRIPE_SECRET_KEY", "DATABASE_URL")}
    try:
        os.environ["DATABASE_URL"] = ""
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_abc"
        assert ProductionPayments(D()).mode == "test"
        assert ProductionPayments(D()).live_ready is False
        os.environ["STRIPE_SECRET_KEY"] = "sk_live_abc"
        assert ProductionPayments(D()).mode == "live"
        os.environ["STRIPE_SECRET_KEY"] = "rk_live_abc"
        assert ProductionPayments(D()).mode == "live"
        os.environ["STRIPE_SECRET_KEY"] = "rk_test_abc"
        assert ProductionPayments(D()).mode == "test"
        assert ProductionPayments(D()).live_ready is False
        os.environ["STRIPE_SECRET_KEY"] = ""
        assert ProductionPayments(D()).mode == "unconfigured"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
