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
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-1"},
        headers=_auth(kb)).json()
    # retry with the same key: same order, no second escrow hold
    p2 = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-1"},
        headers=_auth(kb)).json()
    assert p1["order_id"] == p2["order_id"]
    bal1 = client.get(f"/ledger/{b}", headers=_auth(kb)).json()["balance"]
    assert bal1 == bal0 - 30
    # a new key is a new purchase
    p3 = client.post("/world/services/purchase", json={
        "buyer_id": b, "service_id": svc["id"], "idempotency_key": "idem-2"},
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
        "buyer_id": b1, "service_id": svc["id"], "idempotency_key": "shared"},
        headers=_auth(k1)).json()
    o2 = client.post("/world/services/purchase", json={
        "buyer_id": b2, "service_id": svc["id"], "idempotency_key": "shared"},
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
