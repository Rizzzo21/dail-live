"""Real-money Stripe refund rail — end-to-end tests (bnty_0162 / bnty_0131).

Drives the real FastAPI app through the real HTTP routes with a fake `stripe`
module and a throwaway SQLite database, so the whole refund path is exercised
without touching Stripe:

    POST /payments/checkout -> webhook credit -> POST /payments/refund
      -> admin approval -> stripe.Refund.create -> ledger debit -> audit log

FAIL->PASS: on the unpatched service (no refund rail) every test here fails at
the first refund call with HTTP 404 ("no route for POST /payments/refund"); on
the patched service they pass. Run the same file against a pristine checkout to
see the FAIL half.

The fake Stripe module records every Refund.create call, so the tests can
assert *exactly one* Stripe refund per logical refund (the double-refund bug
this rail exists to prevent) and the exact amount in cents.
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

# --------------------------------------------------------------- fake stripe
# The refund rail needs three Stripe surfaces: checkout session create +
# retrieve, webhook signature construction, and Refund.create. The fake keeps
# every call in-process and records refund attempts for assertions.
fake_stripe = types.ModuleType("stripe")
fake_stripe.api_key = None

_sessions = {}
_idem_sessions = {}
REFUND_CALLS = []          # every Refund.create attempt, in order
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
        REFUND_CALLS.append({"payment_intent": payment_intent,
                             "amount": amount,
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

DB = "/tmp/dail_refund_rail_test.db"
_ENV = {
    "DAIL_REAL_PAYMENTS": "true",
    "STRIPE_SECRET_KEY": "sk_test_fake",
    "STRIPE_WEBHOOK_SECRET": "whsec_fake",
    "DATABASE_URL": f"sqlite:///{DB}",
    "DAIL_PER_USD": "1",
    "DAIL_ADMIN_KEY": "test-admin-key",
}
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


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
    # Re-bind OUR fake stripe right before importing dail: another test
    # module (e.g. test_webhook_ordering.py) may have replaced
    # sys.modules["stripe"] at import time, and dail.refunds binds `stripe`
    # at module load.
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


def _topup(client, aid, key, usd_cents=500):
    """checkout -> paid webhook; returns the session id (DAIL credited)."""
    body = {"agent_id": aid, "usd_cents": usd_cents,
            "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
            "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
    r = client.post("/payments/checkout", json=body, headers=_auth(key))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    payload = json.dumps({"type": "checkout.session.completed",
                          "data": {"object": {"id": sid,
                                              "payment_intent": f"pi_for_{sid}"}}}).encode()
    r = client.post("/payments/webhook", content=payload,
                    headers={"stripe-signature": "valid-signature"})
    assert r.status_code == 200 and r.json()["handled"] is True, r.text
    return sid


def _balance(client, aid, key):
    r = client.get(f"/ledger/{aid}", headers=_auth(key))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def _refund(client, aid, key, sid, key_idem, amount=None, headers=None, reason="test"):
    body = {"agent_id": aid, "session_id": sid, "reason": reason,
            "idempotency_key": key_idem}
    if amount is not None:
        body["amount_dail"] = amount
    return client.post("/payments/refund", json=body,
                       headers=headers if headers is not None else _auth(key))


# --------------------------------------------------------------------- tests
def test_full_refund_end_to_end(client):
    """$5 top-up -> 5 DAIL -> agent requests, admin approves, DAIL reversed."""
    key = _agent(client, "rf_full")
    start = _balance(client, "rf_full", key)
    sid = _topup(client, "rf_full", key, 500)
    assert _balance(client, "rf_full", key) == start + 5

    n_before = len(REFUND_CALLS)
    r = _refund(client, "rf_full", key, sid, "rf-full-0001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval", body
    # Approval gate: no money moved yet, no Stripe call made.
    assert _balance(client, "rf_full", key) == start + 5
    assert len(REFUND_CALLS) == n_before
    rid = body["refund_id"]

    r = client.post(f"/admin/payments/refunds/{rid}/approve", headers=ADMIN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "refunded", done
    assert done["amount_dail"] == 5 and done["usd_cents"] == 500, done
    assert done["stripe_refund_id"] and done["ledger_tx_id"], done
    assert _balance(client, "rf_full", key) == start
    assert len(REFUND_CALLS) == n_before + 1
    assert REFUND_CALLS[-1]["amount"] == 500


def test_replay_same_idempotency_key_never_double_refunds(client):
    """Same idempotency_key replayed: one refund row, one Stripe refund, one debit."""
    key = _agent(client, "rf_idem")
    start = _balance(client, "rf_idem", key)
    sid = _topup(client, "rf_idem", key, 400)
    n = len(REFUND_CALLS)

    a = _refund(client, "rf_idem", key, sid, "rf-idem-0001")
    assert a.status_code == 200, a.text
    rid = a.json()["refund_id"]
    client.post(f"/admin/payments/refunds/{rid}/approve", headers=ADMIN)
    bal_after = _balance(client, "rf_idem", key)
    assert bal_after == start

    # Replay the identical request twice.
    for _ in range(2):
        b = _refund(client, "rf_idem", key, sid, "rf-idem-0001")
        assert b.status_code == 200, b.text
        assert b.json()["refund_id"] == rid, b.json()
        assert b.json()["status"] == "refunded"
    assert _balance(client, "rf_idem", key) == bal_after, "double-debited on replay"
    assert len(REFUND_CALLS) == n + 1, "double Stripe refund on replay"


def test_partial_refunds_until_cumulative_cap(client):
    """Partials sum to the credited amount; the next one is refused."""
    key = _agent(client, "rf_part")
    start = _balance(client, "rf_part", key)
    sid = _topup(client, "rf_part", key, 500)          # 5 DAIL
    for amt, tag in ((3, "rf-part-0001"), (2, "rf-part-0002")):
        r = _refund(client, "rf_part", key, sid, tag, amount=amt)
        assert r.status_code == 200, r.text
        client.post(f"/admin/payments/refunds/{r.json()['refund_id']}/approve",
                    headers=ADMIN)
    assert _balance(client, "rf_part", key) == start
    # Third partial: nothing left refundable on this session.
    r = _refund(client, "rf_part", key, sid, "rf-part-0003", amount=1)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "nothing_to_refund", r.json()
    # Over-asking in one shot is refused too.
    sid2 = _topup(client, "rf_part", key, 200)
    r = _refund(client, "rf_part", key, sid2, "rf-part-0004", amount=9)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "exceeds_refundable", r.json()


def test_spent_dail_blocks_refund_and_reports_numbers(client):
    """DAIL already spent: 409 with the spendable/refundable numbers."""
    key = _agent(client, "rf_spent")
    sid = _topup(client, "rf_spent", key, 300)          # +3 DAIL
    _agent(client, "rf_vendor")
    # Spend down to 1 DAIL with a real trade (buyer = rf_spent).
    bal = _balance(client, "rf_spent", key)
    r = client.post("/world/trades", headers=_auth(key), json={
        "seller_id": "rf_vendor", "buyer_id": "rf_spent", "amount": bal - 1,
        "item": "sink", "idempotency_key": "rf-spend-0001"})
    assert r.status_code == 200, r.text
    spendable = _balance(client, "rf_spent", key)
    assert spendable == 1, spendable
    r = _refund(client, "rf_spent", key, sid, "rf-spent-0001", amount=3)
    assert r.status_code == 409, r.text
    j = r.json()
    assert j["error"] == "insufficient_unrefunded_balance", j
    assert j["spendable"] == spendable and j["refundable_now"] == spendable, j
    assert "escrowed" in j


def test_escrow_is_not_refundable(client):
    """Funds locked in a bounty escrow are never clawed back."""
    key = _agent(client, "rf_escrow")
    sid = _topup(client, "rf_escrow", key, 400)         # +4 DAIL
    bal = _balance(client, "rf_escrow", key)
    escrow_amt = bal - 1
    r = client.post("/world/bounties", headers=_auth(key), json={
        "agent_id": "rf_escrow", "title": "escrow sink",
        "description": "hold funds", "reward": escrow_amt})
    assert r.status_code in (200, 201), r.text
    spendable = _balance(client, "rf_escrow", key)
    assert spendable == 1, spendable
    # Ask for the full refundable 4 DAIL; only 1 is unspent, the rest is escrow.
    r = _refund(client, "rf_escrow", key, sid, "rf-esc-0001", amount=4)
    assert r.status_code == 409, r.text
    j = r.json()
    assert j["error"] == "insufficient_unrefunded_balance", j
    assert j["escrowed"] == escrow_amt, j
    assert j["spendable"] == spendable, j


def test_refund_ownership_and_unknown_session(client):
    key_a = _agent(client, "rf_owner_a")
    key_b = _agent(client, "rf_owner_b")
    sid = _topup(client, "rf_owner_a", key_a, 200)
    # B may not refund A's session (agent-scoped).
    r = _refund(client, "rf_owner_a", key_b, sid, "rf-own-0001")
    assert r.status_code == 403, r.text
    assert "not_your_agent" in r.text, r.text
    # Unknown session -> 404.
    r = _refund(client, "rf_owner_a", key_a, "cs_test_nope", "rf-own-0002")
    assert r.status_code == 404, r.text
    assert r.json()["error"] == "unknown_session"
    # No auth at all -> 401.
    r = client.post("/payments/refund", json={
        "agent_id": "rf_owner_a", "session_id": sid,
        "idempotency_key": "rf-own-0003"})
    assert r.status_code == 401, r.text


def test_unpaid_session_cannot_be_refunded(client):
    key = _agent(client, "rf_unpaid")
    body = {"agent_id": "rf_unpaid", "usd_cents": 500,
            "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
            "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
    sid = client.post("/payments/checkout", json=body, headers=_auth(key)).json()["session_id"]
    r = _refund(client, "rf_unpaid", key, sid, "rf-unpaid-0001")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "nothing_to_refund", r.json()


def test_payment_intent_recovered_offline(client):
    """If the payment_intent column was never recorded, the rail recovers it
    from Stripe (checkout.Session.retrieve) instead of failing."""
    key = _agent(client, "rf_offline")
    sid = _topup(client, "rf_offline", key, 200)
    import sqlalchemy
    eng = sqlalchemy.create_engine(_ENV["DATABASE_URL"])
    with eng.begin() as c:
        c.execute(sqlalchemy.text("UPDATE dail_payments SET payment_intent=NULL "
                                  "WHERE session_id=:s"), {"s": sid})
    n = len(REFUND_CALLS)
    r = _refund(client, "rf_offline", key, sid, "rf-off-0001")
    assert r.status_code == 200, r.text
    client.post(f"/admin/payments/refunds/{r.json()['refund_id']}/approve", headers=ADMIN)
    assert len(REFUND_CALLS) == n + 1
    # Recovered from stripe.checkout.Session.retrieve (the fake's pi_test_NNNN).
    assert REFUND_CALLS[-1]["payment_intent"], REFUND_CALLS[-1]
    assert REFUND_CALLS[-1]["payment_intent"].startswith("pi_test_"), REFUND_CALLS[-1]


def test_charge_refunded_webhook_reconciles_dashboard_refund(client):
    """A refund made in the Stripe dashboard (invisible to our endpoint) still
    debits the DAIL, exactly once, via charge.refunded."""
    key = _agent(client, "rf_dash")
    start = _balance(client, "rf_dash", key)
    sid = _topup(client, "rf_dash", key, 300)           # +3 DAIL
    pi = f"pi_for_{sid}"
    payload = json.dumps({"type": "charge.refunded",
                          "data": {"object": {"payment_intent": pi,
                                              "amount_refunded": 300}}}).encode()
    r = client.post("/payments/webhook", content=payload,
                    headers={"stripe-signature": "valid-signature"})
    assert r.status_code == 200, r.text
    assert r.json()["reconciled"] is True, r.json()
    assert _balance(client, "rf_dash", key) == start
    # Replay the event: no second debit.
    r2 = client.post("/payments/webhook", content=payload,
                     headers={"stripe-signature": "valid-signature"})
    assert r2.status_code == 200
    assert r2.json()["reconciled"] is False, r2.json()
    assert _balance(client, "rf_dash", key) == start


def test_admin_force_is_the_only_negative_balance_path(client):
    """Agent path fails closed; the audited admin force may push negative."""
    key = _agent(client, "rf_force")
    _agent(client, "rf_vendor2")
    sid = _topup(client, "rf_force", key, 500)          # +5 DAIL
    bal = _balance(client, "rf_force", key)
    client.post("/world/trades", headers=_auth(key), json={
        "seller_id": "rf_vendor2", "buyer_id": "rf_force", "amount": bal - 1,
        "item": "sink2", "idempotency_key": "rf-force-spend-1"})
    assert _balance(client, "rf_force", key) == 1
    # Agent path: refused (needs 5, only 1 unspent).
    r = _refund(client, "rf_force", key, sid, "rf-force-0001", amount=5)
    assert r.status_code == 409 and r.json()["error"] == "insufficient_unrefunded_balance"
    # Admin force: allowed, flagged, and audited.
    r = client.post("/admin/payments/refunds/force", headers=ADMIN, json={
        "agent_id": "rf_force", "session_id": sid, "amount_dail": 5,
        "reason": "chargeback", "idempotency_key": "rf-force-0002"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refunded" and r.json()["forced"] is True, r.json()
    assert _balance(client, "rf_force", key) == -4
    audit = client.get("/audit/verify")
    assert audit.status_code == 200
    assert audit.json().get("valid") is True, audit.json()


def test_refund_listing_is_scoped(client):
    key = _agent(client, "rf_list")
    sid = _topup(client, "rf_list", key, 100)
    r = _refund(client, "rf_list", key, sid, "rf-list-0001")
    rid = r.json()["refund_id"]
    mine = client.get("/payments/refunds?agent_id=rf_list", headers=_auth(key))
    assert mine.status_code == 200, mine.text
    assert any(x["refund_id"] == rid for x in mine.json()["refunds"])
    # Another agent cannot read this agent's refunds.
    other = _agent(client, "rf_list_other")
    nope = client.get("/payments/refunds?agent_id=rf_list", headers=_auth(other))
    assert nope.status_code == 403, nope.text
    # Admin sees everything.
    allr = client.get("/payments/refunds", headers=ADMIN)
    assert allr.status_code == 200 and len(allr.json()["refunds"]) >= 1


def test_refund_route_is_in_the_openapi_contract(client):
    """The endpoint is discoverable, not a hidden back door."""
    spec = client.get("/openapi.json").json()
    assert "/payments/refund" in spec["paths"], "refund route missing from openapi"
    assert "/payments/refunds" in spec["paths"]
    assert "/admin/payments/refunds/{refund_id}/approve" in spec["paths"]


# ------------------------------------------------- bnty_0227 regression tests
def test_checkout_rejects_non_multiple_of_100_cents(client):
    """bnty_0227 finding 2: usd_cents=199 used to credit 1 DAIL and strand
    99 cents in Stripe (the refund rail only moves whole DAIL). Now 422."""
    key = _agent(client, "rf_cents")
    body = {"agent_id": "rf_cents", "usd_cents": 199,
            "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
            "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
    r = client.post("/payments/checkout", json=body, headers=_auth(key))
    assert r.status_code == 422, r.text
    assert "multiple of 100" in r.text
    # Multiples of 100 still work.
    body["usd_cents"] = 200
    r = client.post("/payments/checkout", json=body, headers=_auth(key))
    assert r.status_code == 200, r.text


def test_idempotency_key_namespaced_per_agent(client):
    """bnty_0227 finding 1: the raw caller key must never reach Stripe —
    two agents using the same key get different Stripe keys, so one
    agent's key cannot collide with another's."""
    ka = _agent(client, "rf_idem_a")
    kb = _agent(client, "rf_idem_b")
    seen = []

    orig_cm = _SessionAPI.__dict__["create"]  # the classmethod object
    orig_fn = orig_cm.__func__  # underlying function for direct calls

    @classmethod
    def spy(cls, **kwargs):
        seen.append(kwargs.get("idempotency_key"))
        return orig_fn(cls, **kwargs)

    _SessionAPI.create = spy
    try:
        for aid, key in (("rf_idem_a", ka), ("rf_idem_b", kb)):
            body = {"agent_id": aid, "usd_cents": 100,
                    "idempotency_key": "shared-key-0001",
                    "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
                    "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
            r = client.post("/payments/checkout", json=body, headers=_auth(key))
            assert r.status_code == 200, r.text
    finally:
        _SessionAPI.create = orig_cm
    assert len(seen) == 2 and seen[0] and seen[1]
    assert seen[0] != "shared-key-0001", "raw caller key reached Stripe"
    assert seen[0] != seen[1], "cross-agent idempotency key collision"


def test_stripe_idempotency_error_maps_to_409(client, monkeypatch):
    """bnty_0227 finding 1: a replayed key with different parameters is a
    409 Conflict — never a 500 leaking Stripe's internal request id."""
    key = _agent(client, "rf_409")

    class FakeIdempotencyError(Exception):
        pass
    FakeIdempotencyError.__name__ = "IdempotencyError"

    def boom(**kwargs):
        raise FakeIdempotencyError(
            "Request req_test123: Keys for idempotent requests can only be "
            "used with the same parameters they were first used with.")

    monkeypatch.setattr(_SessionAPI, "create", classmethod(lambda cls, **kw: boom(**kw)))
    body = {"agent_id": "rf_409", "usd_cents": 100,
            "idempotency_key": "conflict-key-0001",
            "success_url": "https://dail-3dci.onrender.com/launch?payment=success",
            "cancel_url": "https://dail-3dci.onrender.com/launch?payment=cancelled"}
    r = client.post("/payments/checkout", json=body, headers=_auth(key))
    assert r.status_code == 409, r.text
    assert "req_test123" not in r.text, "Stripe request id leaked"


def test_refunds_list_defaults_to_caller(client):
    """bnty_0227 finding 3: omitting agent_id defaults to the caller (the
    OpenAPI contract marks it optional) instead of 403ing."""
    key = _agent(client, "rf_def")
    sid = _topup(client, "rf_def", key, 100)
    _refund(client, "rf_def", key, sid, "rf-def-0001")
    r = client.get("/payments/refunds", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert any(x["session_id"] == sid for x in r.json()["refunds"])
    # Explicit other-agent access still 403s with the legacy error strings.
    nope = client.get("/payments/refunds?agent_id=rf_list", headers=_auth(key))
    assert nope.status_code == 403 and "not_your_agent" in nope.text


def test_stripe_webhook_alias_and_sanitized_400(client):
    """bnty_0227 finding 4: /stripe/webhook routes to the same
    signature-verified handler (no 401 trap), and a bad signature yields
    a fixed 400 message instead of the raw Stripe SDK string."""
    r = client.post("/stripe/webhook", content=b"{}",
                    headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid webhook signature"
    assert "No signatures found" not in r.text
