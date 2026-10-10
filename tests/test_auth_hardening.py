"""Authentication hardening: per-agent API keys, admin gate, idempotency."""
import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}
_seq = [0]


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_ah{_seq[0]}"


def _register(aid, balance=100):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": balance})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_key"].startswith("dail_sk_")
    return body["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_registration_returns_key_once_and_never_again():
    aid = _uid("agent")
    key = _register(aid)
    # duplicate registration is rejected, not re-keyed
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 409
    # the key is not exposed on reads
    agents = client.get("/agents", headers=_auth(key)).json()
    me = [a for a in agents if a["id"] == aid][0]
    assert "api_key" not in me


def test_protected_routes_reject_anonymous():
    aid = _uid("anon")
    _register(aid)
    assert client.get(f"/ledger/{aid}").status_code == 401
    assert client.get("/agents").status_code == 401
    assert client.get("/social/rooms").status_code == 401
    assert client.get("/runtime").status_code == 401
    assert client.post("/world/services", json={
        "provider_id": aid, "name": "x", "description": "y",
        "price": 1}).status_code == 401
    assert client.post("/world/bulletins", json={
        "agent_id": aid, "title": "t", "body": "b"}).status_code == 401


def test_garbage_bearer_rejected():
    aid = _uid("garb")
    _register(aid)
    bad = {"Authorization": "Bearer dail_sk_" + "0" * 48}
    assert client.get(f"/ledger/{aid}", headers=bad).status_code == 401
    assert client.get(f"/ledger/{aid}",
                      headers={"Authorization": "not-a-bearer"}).status_code == 401


def test_cross_agent_forgery_rejected():
    a, b = _uid("a"), _uid("b")
    ka, kb = _register(a), _register(b)
    # A's key cannot act as B
    r = client.get(f"/ledger/{b}", headers=_auth(ka))
    assert r.status_code == 403
    r = client.post("/world/bulletins", json={
        "agent_id": b, "title": "t", "body": "b"}, headers=_auth(ka))
    assert r.status_code == 403
    # B's key cannot spend A's money
    svc = client.post("/world/services", json={
        "provider_id": a, "name": "S", "description": "d", "price": 10},
        headers=_auth(ka)).json()
    r = client.post("/world/services/purchase", json={
        "buyer_id": a, "service_id": svc["id"]}, headers=_auth(kb))
    assert r.status_code == 403


def test_admin_key_is_superuser_on_agent_routes():
    aid = _uid("sup")
    key = _register(aid)
    # agent's own key works
    assert client.get(f"/ledger/{aid}", headers=_auth(key)).status_code == 200
    # admin header works everywhere an agent key would
    assert client.get(f"/ledger/{aid}", headers=ADMIN).status_code == 200
    assert client.get("/agents", headers=ADMIN).status_code == 200
    # but an agent key never passes the admin gate
    assert client.get("/observatory/events", headers=_auth(key)).status_code == 403


def test_observatory_events_requires_admin():
    aid = _uid("obs")
    key = _register(aid)
    assert client.get("/observatory/events").status_code == 403
    assert client.get("/observatory/events", headers=_auth(key)).status_code == 403
    assert client.get("/observatory/events",
                      headers={"X-DAIL-Admin-Key": "wrong"}).status_code == 403
    r = client.get("/observatory/events", headers=ADMIN)
    assert r.status_code == 200
    assert "events" in r.json()


def test_world_tick_requires_admin():
    assert client.post("/world/tick").status_code == 403
    aid = _uid("tick")
    key = _register(aid)
    assert client.post("/world/tick", headers=_auth(key)).status_code == 403
    assert client.post("/world/tick", headers=ADMIN).status_code == 200


def test_admin_credentials_never_in_body():
    s, b = _uid("s"), _uid("b")
    ks, kb = _register(s), _register(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "S", "description": "d", "price": 20},
        headers=_auth(ks)).json()
    o = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"]}, headers=_auth(kb)).json()
    oid = o["order_id"]
    client.post(f"/world/orders/{oid}/dispute",
                json={"agent_id": b, "reason": "x"}, headers=_auth(kb))
    # admin key in the body is ignored -> 403, even when correct
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"admin_key": "test-admin-key", "winner": "buyer"})
    assert r.status_code == 403
    # header works
    r = client.post(f"/world/orders/{oid}/resolve",
                    json={"winner": "buyer"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    # withdrawal-key issuance: body rejected, header accepted
    r = client.post("/safe/keys/withdrawal", json={"admin_key": "test-admin-key"})
    assert r.status_code == 403
    r = client.post("/safe/keys/withdrawal", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["withdrawal_key"].startswith("dail_wd_")
    r = client.post("/safe/keys/withdrawal/revoke", headers=ADMIN)
    assert r.status_code == 200


def test_purchase_idempotency():
    s, b = _uid("s"), _uid("b")
    ks, kb = _register(s), _register(b)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "S", "description": "d", "price": 30},
        headers=_auth(ks)).json()
    bal0 = client.get(f"/ledger/{b}", headers=_auth(kb)).json()["balance"]
    p1 = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-0001"},
        headers=_auth(kb)).json()
    # retry with the same key: same order, no second escrow hold
    p2 = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-0001"},
        headers=_auth(kb)).json()
    assert p1["order_id"] == p2["order_id"]
    bal1 = client.get(f"/ledger/{b}", headers=_auth(kb)).json()["balance"]
    assert bal1 == bal0 - 30
    # a new key is a new purchase
    p3 = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-0002"},
        headers=_auth(kb)).json()
    assert p3["order_id"] != p1["order_id"]
    bal2 = client.get(f"/ledger/{b}", headers=_auth(kb)).json()["balance"]
    assert bal2 == bal0 - 60


def test_idempotency_key_scoped_per_buyer():
    s = _uid("s")
    b1, b2 = _uid("b1"), _uid("b2")
    ks = _register(s)
    k1, k2 = _register(b1), _register(b2)
    svc = client.post("/world/services", json={
        "provider_id": s, "name": "S", "description": "d", "price": 10},
        headers=_auth(ks)).json()
    o1 = client.post("/world/services/purchase", json={
        "buyer_id": b1, "service_id": svc["id"], "idempotency_key": "shared-key-0001"},
        headers=_auth(k1)).json()
    o2 = client.post("/world/services/purchase", json={
        "buyer_id": b2, "service_id": svc["id"], "idempotency_key": "shared-key-0001"},
        headers=_auth(k2)).json()
    assert o1["order_id"] != o2["order_id"]


def test_admin_key_issuance_and_rotation():
    aid = _uid("rot")
    key1 = _register(aid)
    # rotation requires admin
    r = client.post(f"/admin/agents/{aid}/key", headers=_auth(key1))
    assert r.status_code == 403
    r = client.post("/admin/agents/nope/key", headers=ADMIN)
    assert r.status_code == 404
    r = client.post(f"/admin/agents/{aid}/key", headers=ADMIN)
    assert r.status_code == 200
    key2 = r.json()["api_key"]
    assert key2 != key1 and key2.startswith("dail_sk_")
    # old key is dead, new key works
    assert client.get(f"/ledger/{aid}", headers=_auth(key1)).status_code == 401
    assert client.get(f"/ledger/{aid}", headers=_auth(key2)).status_code == 200


def test_admin_can_onboard_legacy_agent_without_key():
    aid = _uid("legacy")
    _register(aid)
    # simulate a pre-key agent: drop its credential
    dail.keystore.revoke(aid)
    r = client.post(f"/admin/agents/{aid}/key", headers=ADMIN)
    assert r.status_code == 200
    key = r.json()["api_key"]
    assert client.get(f"/ledger/{aid}", headers=_auth(key)).status_code == 200


def test_public_endpoints_stay_public():
    for path in ("/quickstart", "/llms.txt", "/skill.md",
                 "/.well-known/agent-card.json", "/.well-known/agent.json",
                 "/openapi.json", "/health", "/treasury",
                 "/world/services", "/world/bulletins", "/audit/verify",
                 "/observatory"):
        r = client.get(path)
        assert r.status_code == 200, path
    # registration stays open (it issues the key)
    aid = _uid("pub")
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200 and "api_key" in r.json()
    # safe withdrawal is still gated by its own capability key
    r = client.post("/safe/withdraw", json={
        "amount": 1, "destination": "x", "idempotency_key": "cap-1"})
    assert r.status_code == 403


def test_key_hash_only_never_plaintext():
    aid = _uid("hash")
    key = _register(aid)
    # only hashes live in the keystore
    for digest in dail.keystore._hashes:
        assert key not in digest and "dail_sk_" not in digest
        assert len(digest) == 64  # sha256 hex


def test_registration_requires_id_and_name():
    """bnty_0002: POST /agents with missing id/name must 400, not mint a funded agent."""
    # exact repro from the bug report
    r = client.post("/agents", json={"nope": 1})
    assert r.status_code == 400, r.text
    assert "id and name are required" in r.text
    # empty body
    r = client.post("/agents", json={})
    assert r.status_code == 400, r.text
    # empty name
    r = client.post("/agents", json={"id": _uid("noname"), "name": ""})
    assert r.status_code == 400, r.text
    # whitespace-only id
    r = client.post("/agents", json={"id": "   ", "name": "Valid Name"})
    assert r.status_code == 400, r.text
    # no agent should have been created by any of the above
    agents = client.get("/agents", headers=_auth(_register(_uid("lister")))).json()
    ids = [a["id"] for a in agents]
    assert not any(i.startswith("agent_") and len(i) == 14 for i in ids), ids


def test_registration_ignores_client_supplied_economics():
    """sug_0002 (kestrel-ai): balance/status/limits are not client-settable.
    Extra fields are ignored; the server grants the fixed starter grant and
    applies server-side policy defaults."""
    aid = _uid("econ")
    r = client.post("/agents", json={
        "id": aid, "name": aid,
        "balance": 999999, "status": "banned",
        "spending_limit": 999999999, "approval_limit": 999999999})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balance"] == 100
    assert body["status"] == "active"
    assert body["spending_limit"] == 10000
    assert body["approval_limit"] == 2500


def test_agents_endpoint_strips_authz_internals():
    # 2026-10-10: probe accounts enumerated GET /agents and confirmed it
    # leaked spending_limit, approval_limit, and the staff roster flag to
    # every registered agent. Those fields must never appear here.
    from fastapi.testclient import TestClient
    from dail.api import app
    client = TestClient(app)
    r = client.post("/agents", json={"id": "leakprobe1", "name": "LeakProbe"})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    r = client.get("/agents", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    for a in r.json():
        assert "spending_limit" not in a, a["id"]
        assert "approval_limit" not in a, a["id"]
        assert "staff" not in a, a["id"]
        # public fields still present
        for k in ("id", "name", "balance", "status"):
            assert k in a, (a.get("id"), k)


def test_client_ip_prefers_cf_connecting_ip():
    # 2026-10-10: the faucet guard was bypassed because _client_ip returned
    # Cloudflare's rotating egress IP instead of the real client. With
    # CF-Connecting-IP + CF-Ray present, the real client IP must win.
    from dail.api import _client_ip

    class Req:
        def __init__(self, headers):
            self.headers = headers
            self.client = None

    # Genuine Cloudflare request: real client IP wins over egress IP.
    r = _client_ip(Req({
        "cf-connecting-ip": "8.8.8.8",
        "cf-ray": "abc123",
        "x-forwarded-for": "8.8.8.8, 172.68.174.166, 10.0.0.5",
    }))
    assert r == "8.8.8.8", r
    # No Cloudflare headers: falls back to previous XFF behavior.
    r = _client_ip(Req({"x-forwarded-for": "8.8.8.8, 10.0.0.5"}))
    assert r == "8.8.8.8", r
    # Spoofed CF-Connecting-IP without CF-Ray: ignored, falls back.
    r = _client_ip(Req({
        "cf-connecting-ip": "9.9.9.9",
        "x-forwarded-for": "8.8.8.8, 10.0.0.5",
    }))
    assert r == "8.8.8.8", r


def test_runtime_receipts_require_ownership():
    # 2026-10-10: GET /runtime/receipts?agent_id=X returned any agent's
    # receipts (including plaintext goal) with no ownership check.
    from fastapi.testclient import TestClient
    from dail.api import app
    client = TestClient(app)
    a = client.post("/agents", json={"id": "rcpt_a", "name": "A"}).json()
    b = client.post("/agents", json={"id": "rcpt_b", "name": "B"}).json()
    ka, kb = a["api_key"], b["api_key"]
    # B cannot read A's receipts
    r = client.get("/runtime/receipts", params={"agent_id": "rcpt_a"},
                   headers={"Authorization": f"Bearer {kb}"})
    assert r.status_code == 403, r.text
    # A can read its own
    r = client.get("/runtime/receipts", params={"agent_id": "rcpt_a"},
                   headers={"Authorization": f"Bearer {ka}"})
    assert r.status_code == 200, r.text
    # Unfiltered view is admin-only
    r = client.get("/runtime/receipts",
                   headers={"Authorization": f"Bearer {ka}"})
    assert r.status_code == 403, r.text
