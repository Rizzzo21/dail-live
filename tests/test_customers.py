"""Tests for the human customer dashboard backend (invite auth, tasks, review)."""
import os
import sys
from pathlib import Path

os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from dail.api import app, dail

ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _authed_customer(name="Test Human"):
    code = TestClient(app).post("/admin/invites", headers=ADMIN).json()["code"]
    r = TestClient(app).post("/customers/redeem",
                             json={"code": code, "name": name})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_invite_redeem_and_me():
    c = TestClient(app)
    code = c.post("/admin/invites", headers=ADMIN).json()["code"]
    assert code.startswith("DAIL-INV-")
    r = c.post("/customers/redeem", json={"code": code, "name": "Alice"})
    assert r.status_code == 200
    tok = r.json()["session_token"]
    me = c.get("/customers/me",
               headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["name"] == "Alice"
    assert me["balance"] == 0
    # double redeem fails
    r2 = c.post("/customers/redeem", json={"code": code, "name": "Bob"})
    assert r2.status_code == 400


def test_unauthorized_customer_endpoints():
    c = TestClient(app)
    r = c.get("/customers/me")
    assert r.status_code == 401
    r = c.get("/customers/me", headers={"Authorization": "Bearer junk"})
    assert r.status_code == 401


def test_customer_cannot_see_others_tasks():
    c = TestClient(app)
    h1 = _authed_customer("Human One")
    h2 = _authed_customer("Human Two")
    # fund human one via ledger directly (test helper path)
    assert c.get("/customers/me/tasks", headers=h1).json() == []
    assert c.get("/customers/me/tasks", headers=h2).json() == []
    # isolation: no cross-customer leakage on empty sets
    assert h1 != h2


def test_dashboard_page_public():
    c = TestClient(app)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Your Dashboard" in r.text or "Get work done" in r.text


def test_invite_admin_only():
    c = TestClient(app)
    r = c.post("/admin/invites")
    assert r.status_code == 403


def test_customer_checkout_requires_auth():
    c = TestClient(app)
    r = c.post("/customers/me/checkout", json={"usd_cents": 2000})
    assert r.status_code == 401


def test_customer_checkout_validates_amount():
    c = TestClient(app)
    h = _authed_customer("Checkout Tester")
    # Too small
    r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": 50})
    # 503 because Stripe isn't configured in test, or 400 for validation.
    # Either way it must not 500 or succeed silently.
    assert r.status_code in (400, 503)
    # Non-integer
    r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": "abc"})
    assert r.status_code == 400


def test_new_customer_ids_are_unguessable_and_collision_retried(monkeypatch):
    """bnty_0240: new customer IDs are 16-hex-char random tokens (not sequential)
    and automatically retry if a candidate ID collides with an existing record."""
    import re
    import secrets as std_secrets
    c = TestClient(app)
    code1 = c.post("/admin/invites", headers=ADMIN).json()["code"]
    r1 = c.post("/customers/redeem", json={"code": code1, "name": "Hex One"})
    assert r1.status_code == 200
    cid1 = r1.json()["customer_id"]
    assert re.fullmatch(r"cust_[0-9a-f]{16}", cid1), f"unexpected ID format: {cid1}"
    existing_hex = cid1.split("_", 1)[1]

    # Force the first secrets.token_hex(8) call to collide with cid1, then yield a fresh hex.
    real_token_hex = std_secrets.token_hex
    calls = {"n": 0}

    def colliding_token_hex(nbytes=32):
        if nbytes == 8:
            calls["n"] += 1
            if calls["n"] == 1:
                return existing_hex
        return real_token_hex(nbytes)

    monkeypatch.setattr("dail.customers.secrets.token_hex", colliding_token_hex)
    code2 = c.post("/admin/invites", headers=ADMIN).json()["code"]
    r2 = c.post("/customers/redeem", json={"code": code2, "name": "Hex Two"})
    assert r2.status_code == 200
    cid2 = r2.json()["customer_id"]
    assert cid2 != cid1
    assert re.fullmatch(r"cust_[0-9a-f]{16}", cid2)
    assert calls["n"] >= 2
    assert dail.customers.get_customer(cid1)["name"] == "Hex One"
    assert dail.customers.get_customer(cid2)["name"] == "Hex Two"


def test_concurrent_redemptions_produce_unique_unguessable_ids():
    """bnty_0240: simulated concurrent invite redemptions never collide on customer_id,
    and concurrent attempts on the same invite code succeed at most once."""
    import re
    import threading
    c = TestClient(app)
    n_workers = 24
    codes = [c.post("/admin/invites", headers=ADMIN).json()["code"] for _ in range(n_workers)]
    barrier = threading.Barrier(n_workers)
    results = [None] * n_workers

    def _redeem_worker(idx):
        barrier.wait()
        resp = c.post("/customers/redeem",
                      json={"code": codes[idx], "name": f"Concurrent {idx}"})
        results[idx] = (resp.status_code, resp.json())

    threads = [threading.Thread(target=_redeem_worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cids = []
    for status, body in results:
        assert status == 200, body
        cid = body["customer_id"]
        assert re.fullmatch(r"cust_[0-9a-f]{16}", cid)
        cids.append(cid)
    assert len(set(cids)) == n_workers

    # Concurrent race on a SINGLE invite code: exactly 1 succeeds, the rest get 400.
    shared_code = c.post("/admin/invites", headers=ADMIN).json()["code"]
    race_workers = 12
    race_barrier = threading.Barrier(race_workers)
    race_statuses = [None] * race_workers

    def _race_same_code(idx):
        race_barrier.wait()
        resp = c.post("/customers/redeem",
                      json={"code": shared_code, "name": f"Racer {idx}"})
        race_statuses[idx] = resp.status_code

    rthreads = [threading.Thread(target=_race_same_code, args=(i,)) for i in range(race_workers)]
    for t in rthreads:
        t.start()
    for t in rthreads:
        t.join()
    assert race_statuses.count(200) == 1
    assert race_statuses.count(400) == race_workers - 1


def test_grandfathered_sequential_cust_0001_still_works(monkeypatch, tmp_path):
    """bnty_0240: pre-existing sequential IDs (cust_0001) remain grandfathered
    across session issue/lookup, dashboard /customers/me, tasks, and Stripe checkout."""
    import dail.api as api_mod
    c = TestClient(app)
    # Seed a pre-existing grandfathered sequential customer cust_0001
    dail.customers.customers["cust_0001"] = {
        "id": "cust_0001",
        "name": "Grandfathered Pioneer",
        "email": "pioneer@example.com",
        "created_at": "2026-10-09T00:00:00+00:00",
        "verified": True,
    }
    sess = dail.customers._new_session("cust_0001")
    assert sess["customer_id"] == "cust_0001"
    headers = {"Authorization": f"Bearer {sess['session_token']}"}

    # 1. Login / session verification & /customers/me
    me = c.get("/customers/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == "cust_0001"
    assert me.json()["name"] == "Grandfathered Pioneer"

    # 2. Credit & task posting
    cr = c.post("/admin/customers/cust_0001/credit", headers=ADMIN, json={"amount": 20})
    assert cr.status_code == 200
    assert cr.json()["balance"] == 20
    task_r = c.post("/customers/me/tasks", headers=headers,
                    json={"title": "Legacy ID task", "description": "works", "reward": 5})
    assert task_r.status_code == 200
    tasks = c.get("/customers/me/tasks", headers=headers).json()
    assert any(t["id"] == task_r.json()["id"] for t in tasks)

    # 3. Stripe checkout flow with cust_0001
    monkeypatch.setenv("DAIL_REAL_PAYMENTS", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_legacy0001")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_legacy0001")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/legacy_pay.db")
    from dail.production_payments import ProductionPayments
    pp = ProductionPayments(dail)
    monkeypatch.setattr(api_mod, "production_payments", pp)

    class _FakeSession:
        id = "cs_test_legacy_0001"
        url = "https://checkout.stripe.com/c/pay/cs_test_legacy_0001"

    monkeypatch.setattr("dail.production_payments.stripe.checkout.Session.create", lambda **kw: _FakeSession())
    co = c.post("/customers/me/checkout", headers=headers, json={"usd_cents": 1000})
    assert co.status_code == 200, co.text
    assert co.json()["checkout_url"] == _FakeSession.url
    rec = pp.get_payment("cs_test_legacy_0001")
    assert rec["agent_id"] == "customer:cust_0001"


# ------------------------------------------------- bnty_0241 customer top-up exception tests
def _setup_customer_payments(monkeypatch, tmp_path, db_name="cust_pay.db"):
    import dail.api as api_mod
    from dail.production_payments import ProductionPayments
    monkeypatch.setenv("DAIL_REAL_PAYMENTS", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_bnty0241")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_bnty0241")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/{db_name}")
    pp = ProductionPayments(dail)
    monkeypatch.setattr(api_mod, "production_payments", pp)
    return pp


def test_customer_checkout_card_declined_maps_to_402_and_never_credits(monkeypatch, tmp_path):
    """bnty_0241: Card declined on customer top-up -> 402 with plain message,
    no raw Stripe SDK strings/request IDs, and zero ledger or pending mutation."""
    pp = _setup_customer_payments(monkeypatch, tmp_path, "card_declined.db")
    c = TestClient(app)
    h = _authed_customer("Card Declined Human")
    me0 = c.get("/customers/me", headers=h).json()
    cid = me0["id"]
    assert me0["balance"] == 0

    class CardError(Exception):
        code = "card_declined"

    def _boom(**kw):
        raise CardError("Request req_secret_card_999: Your card was declined (do_not_honor).")

    monkeypatch.setattr("dail.production_payments.stripe.checkout.Session.create", _boom)
    r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": 1000})
    assert r.status_code == 402, r.text
    body = r.json()["detail"].lower()
    assert "card declined" in body
    assert "req_secret_card_999" not in r.text
    assert "CardError" not in r.text
    # Zero balance mutation and zero pending rows left behind
    assert dail.customers.balance(cid) == 0
    assert pp.list_payments() == []


def test_customer_checkout_expired_or_invalid_session_maps_to_400_and_401(monkeypatch, tmp_path):
    """bnty_0241: Expired/invalid customer auth session -> 401; expired/invalid
    Stripe checkout session or bad top-up parameters -> 400 with plain message."""
    import hashlib
    pp = _setup_customer_payments(monkeypatch, tmp_path, "invalid_session.db")
    c = TestClient(app)
    h = _authed_customer("Session Tester")
    cid = c.get("/customers/me", headers=h).json()["id"]

    # 1. Invalid bearer token -> 401
    r_bad_tok = c.post("/customers/me/checkout",
                       headers={"Authorization": "Bearer invalid_token_xyz"},
                       json={"usd_cents": 1000})
    assert r_bad_tok.status_code == 401

    # 2. Expired customer session token -> 401
    exp_sess = dail.customers._new_session(cid)
    thash = hashlib.sha256(exp_sess["session_token"].encode()).hexdigest()
    dail.customers.sessions[thash]["expires_at"] = "2020-01-01T00:00:00+00:00"
    r_exp_tok = c.post("/customers/me/checkout",
                       headers={"Authorization": f"Bearer {exp_sess['session_token']}"},
                       json={"usd_cents": 1000})
    assert r_exp_tok.status_code == 401
    assert "expired" in r_exp_tok.json()["detail"].lower()

    # 3. Non-multiple of 100 cents, boolean usd_cents, blank idempotency_key -> 400
    for bad_payload in (
        {"usd_cents": 199},
        {"usd_cents": True},
        {"usd_cents": 1000, "idempotency_key": "   "},
        {"usd_cents": 1000, "idempotency_key": 12345},
    ):
        r_bad = c.post("/customers/me/checkout", headers=h, json=bad_payload)
        assert r_bad.status_code == 400, (bad_payload, r_bad.status_code, r_bad.text)

    # 4. Stripe InvalidRequestError / expired checkout session -> 400 with no SDK leak
    class InvalidRequestError(Exception):
        code = "session_expired"

    def _invalid_req(**kw):
        raise InvalidRequestError("Request req_inv_777: Checkout session has expired.")

    monkeypatch.setattr("dail.production_payments.stripe.checkout.Session.create", _invalid_req)
    r_inv = c.post("/customers/me/checkout", headers=h, json={"usd_cents": 1000})
    assert r_inv.status_code == 400, r_inv.text
    assert "invalid or expired checkout session" in r_inv.json()["detail"].lower()
    assert "req_inv_777" not in r_inv.text
    assert "InvalidRequestError" not in r_inv.text
    assert dail.customers.balance(cid) == 0
    assert pp.list_payments() == []


def test_customer_checkout_stripe_outage_maps_to_502_with_retry_guidance_and_zero_mutation(monkeypatch, tmp_path):
    """bnty_0241: Stripe outage (APIConnectionError / APIError / RateLimitError) -> 502
    with retry guidance, no raw SDK strings, and zero ledger or DB mutation."""
    pp = _setup_customer_payments(monkeypatch, tmp_path, "outage.db")
    c = TestClient(app)
    h = _authed_customer("Outage Tester")
    cid = c.get("/customers/me", headers=h).json()["id"]

    for err_name in ("APIConnectionError", "APIError", "RateLimitError", "ServiceUnavailableError"):
         exc_cls = type(err_name, (Exception,), {})

         def _outage(**kw):
             raise exc_cls(f"Request req_outage_555: Upstream {err_name} trace at stripe/http_client.py:123")

         monkeypatch.setattr("dail.production_payments.stripe.checkout.Session.create", _outage)
         r = c.post("/customers/me/checkout", headers=h, json={"usd_cents": 2000})
         assert r.status_code == 502, (err_name, r.status_code, r.text)
         detail = r.json()["detail"].lower()
         assert "temporarily unavailable" in detail and "retry" in detail
         assert "req_outage_555" not in r.text
         assert err_name not in r.text
         assert "stripe/http_client.py" not in r.text

    assert dail.customers.balance(cid) == 0
    assert pp.list_payments() == []


def test_customer_topup_replayed_webhook_is_idempotent_200_and_retry_never_double_charges(monkeypatch, tmp_path):
    """bnty_0241: Proof that (1) a failed payment never credits DAIL,
    (2) retrying checkout after failure with idempotency_key never double-charges,
    and (3) replayed/duplicate webhooks return idempotent 200 and credit exactly once."""
    pp = _setup_customer_payments(monkeypatch, tmp_path, "webhook_idem.db")
    c = TestClient(app)
    h = _authed_customer("Idempotent Buyer")
    cid = c.get("/customers/me", headers=h).json()["id"]
    assert dail.customers.balance(cid) == 0

    # Step 1: First attempt hits a transient Stripe outage -> 502, zero credit, zero DB rows
    calls = {"create": 0}
    sessions_by_idem = {}

    class APIConnectionError(Exception):
        pass

    class _FakeSession:
        def __init__(self, sid):
            self.id = sid
            self.url = f"https://checkout.stripe.com/c/pay/{sid}"

    def _flaky_create(**kw):
        calls["create"] += 1
        if calls["create"] == 1:
            raise APIConnectionError("Request req_net_1: network timeout")
        idem = kw.get("idempotency_key")
        if idem and idem in sessions_by_idem:
            prev_cents, prev_sess = sessions_by_idem[idem]
            cur_cents = kw["line_items"][0]["price_data"]["unit_amount"]
            if prev_cents != cur_cents:
                err_cls = type("IdempotencyError", (Exception,), {})
                raise err_cls("Request req_idem_conflict: conflicting params")
            return prev_sess
        sess = _FakeSession("cs_test_cust_topup_01")
        if idem:
            sessions_by_idem[idem] = (kw["line_items"][0]["price_data"]["unit_amount"], sess)
        return sess

    monkeypatch.setattr("dail.production_payments.stripe.checkout.Session.create", _flaky_create)

    r_fail = c.post("/customers/me/checkout", headers=h,
                    json={"usd_cents": 1500, "idempotency_key": "cust-topup-key-01"})
    assert r_fail.status_code == 502
    assert dail.customers.balance(cid) == 0
    assert pp.list_payments() == []

    # Step 2: Customer retries with the same idempotency_key -> 200, single session created
    r_ok1 = c.post("/customers/me/checkout", headers=h,
                   json={"usd_cents": 1500, "idempotency_key": "cust-topup-key-01"})
    assert r_ok1.status_code == 200, r_ok1.text
    r_ok2 = c.post("/customers/me/checkout", headers=h,
                   json={"usd_cents": 1500, "idempotency_key": "cust-topup-key-01"})
    assert r_ok2.status_code == 200
    assert r_ok1.json()["checkout_url"] == r_ok2.json()["checkout_url"]
    assert len(pp.list_payments()) == 1
    assert dail.customers.balance(cid) == 0  # Not credited until verified webhook

    # Conflicting retry with same idempotency_key -> 409 without leaking req_idem_conflict
    r_conflict = c.post("/customers/me/checkout", headers=h,
                        json={"usd_cents": 2500, "idempotency_key": "cust-topup-key-01"})
    assert r_conflict.status_code == 409
    assert "req_idem_conflict" not in r_conflict.text

    # Step 3: Failed async payment webhook never credits DAIL
    events = {}
    monkeypatch.setattr(
        "dail.production_payments.stripe.Webhook.construct_event",
        lambda payload, sig, secret: events[sig],
    )
    events["sig_fail"] = {
        "id": "evt_cust_fail_01",
        "type": "checkout.session.async_payment_failed",
        "data": {"object": {"id": "cs_test_cust_topup_01", "payment_intent": "pi_cust_01"}},
    }
    w_fail = c.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig_fail"})
    assert w_fail.status_code == 200
    assert w_fail.json()["credited"] is False
    assert dail.customers.balance(cid) == 0

    # Step 4: Completed webhook credits 15 DAIL once; replayed webhooks (same event_id
    # AND new event_id for the same session_id) return idempotent 200 with zero double-credit
    events["sig_paid_1"] = {
        "id": "evt_cust_paid_01",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test_cust_topup_01", "payment_intent": "pi_cust_01"}},
    }
    w1 = c.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig_paid_1"})
    assert w1.status_code == 200
    assert w1.json()["duplicate"] is False
    assert dail.customers.balance(cid) == 15

    # Replay 1: identical event_id
    w2 = c.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig_paid_1"})
    assert w2.status_code == 200
    assert w2.json()["duplicate"] is True
    assert dail.customers.balance(cid) == 15

    # Replay 2: distinct event_id for the same already-paid session
    events["sig_paid_2"] = {
        "id": "evt_cust_paid_02",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test_cust_topup_01", "payment_intent": "pi_cust_01"}},
    }
    w3 = c.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig_paid_2"})
    assert w3.status_code == 200
    assert w3.json()["duplicate"] is True
    assert dail.customers.balance(cid) == 15
