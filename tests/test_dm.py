"""Private DMs: DAiL Concierge <-> agent.

Admin side (/admin/dm/*) is the owner's private line from the Observatory;
the agent side (/dm/thread + /dm/reply) uses Bearer auth. Both free.
Threads persist via dail_kv.
"""
import os
import sys
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
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_dm{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_admin_dm_endpoints_require_admin_key():
    for method, url in [("post", "/admin/dm/send"),
                        ("get", "/admin/dm/threads")]:
        r = client.request(method, url, json={} if method == "post" else None)
        assert r.status_code == 403, (url, r.text)
        r = client.request(method, url, json={} if method == "post" else None,
                           headers={"X-DAIL-Admin-Key": "wrong-key"})
        assert r.status_code == 403, (url, r.text)
    r = client.get("/admin/dm/someone")
    assert r.status_code == 403, r.text


def test_dm_full_roundtrip_and_unread_counts():
    a = _make(_uid("agent"))
    # Admin sends -> agent has unread
    r = client.post("/admin/dm/send", json={"agent_id": a, "message": "hey, take a look at bnty_0051"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["message"]["from_id"] == "dail_concierge"
    assert body["message"]["from_name"] == "DAiL Concierge"
    # Agent reads their thread; unread clears
    r = client.get("/dm/thread", headers=_auth(a))
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert len(msgs) == 1 and msgs[0]["message"] == "hey, take a look at bnty_0051"
    # Agent replies -> admin has unread
    r = client.post("/dm/reply", json={"message": "on it, claiming now"}, headers=_auth(a))
    assert r.status_code == 201, r.text
    assert r.json()["message"]["from_id"] == a
    # Admin thread list shows 1 unread
    r = client.get("/admin/dm/threads", headers=ADMIN)
    assert r.status_code == 200, r.text
    threads = r.json()["threads"]
    mine = [t for t in threads if t["agent_id"] == a][0]
    assert mine["unread_count"] == 1, mine
    assert mine["message_count"] == 2
    # Admin reads the thread; unread clears
    r = client.get(f"/admin/dm/{a}", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert len(r.json()["messages"]) == 2
    r = client.get("/admin/dm/threads", headers=ADMIN)
    mine = [t for t in r.json()["threads"] if t["agent_id"] == a][0]
    assert mine["unread_count"] == 0, mine


def test_dm_agent_cannot_read_other_thread():
    a = _make(_uid("agent"))
    b = _make(_uid("agent"))
    r = client.post("/admin/dm/send", json={"agent_id": a, "message": "secret task"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    # B's own thread is empty and does not leak A's messages
    r = client.get("/dm/thread", headers=_auth(b))
    assert r.status_code == 200, r.text
    assert r.json()["messages"] == []
    # No agent-auth -> rejected (401 from the middleware gate)
    r = client.get("/dm/thread")
    assert r.status_code in (401, 403), r.text
    r = client.post("/dm/reply", json={"message": "hi"})
    assert r.status_code in (401, 403), r.text


def test_dm_validation_and_unknown_agent():
    a = _make(_uid("agent"))
    # Empty message rejected
    r = client.post("/admin/dm/send", json={"agent_id": a, "message": ""}, headers=ADMIN)
    assert r.status_code in (400, 422), r.text
    # Over-long rejected by the model
    r = client.post("/admin/dm/send", json={"agent_id": a, "message": "x" * 2001}, headers=ADMIN)
    assert r.status_code in (400, 422), r.text
    # Unknown agent -> 404
    r = client.post("/admin/dm/send", json={"agent_id": "nope_nobody", "message": "hi"}, headers=ADMIN)
    assert r.status_code == 404, r.text
    r = client.get("/admin/dm/nope_nobody", headers=ADMIN)
    assert r.status_code == 404, r.text


def test_dm_persists_through_kv_roundtrip():
    # Simulate persistence with a stub kv store: every dm write must
    # kv_set("dm_threads", ...), and a fresh SocialWorld seeded from that
    # data must recover the thread (restart survival).
    from dail.service import SocialWorld
    kv = {}

    class StubStore:
        enabled = True
        def kv_set(self, k, v):
            import copy
            kv[k] = copy.deepcopy(v)
        def kv_get(self, k, default=None):
            return kv.get(k, default)

    store = StubStore()
    sw = SocialWorld(dail.ledger, dail.audit, store)
    agent_stub = type("A", (), {"id": "dm_persist_agent", "name": "Persisty"})()
    sw.register(agent_stub)
    sw.dm_send("dm_persist_agent", "persist me")
    saved = kv.get("dm_threads")
    assert saved and "dm_persist_agent" in saved, kv.keys()
    # Restart: fresh SocialWorld, threads re-seeded from kv (as _restore_world does).
    # Identities rebuild from the agent table in production; stub it here.
    fresh = SocialWorld(dail.ledger, dail.audit, store)
    fresh.register(agent_stub)
    fresh.dm_threads = kv.get("dm_threads") or {}
    t = fresh.dm_thread_agent("dm_persist_agent")
    assert len(t["messages"]) == 1 and t["messages"][0]["message"] == "persist me"
    # Unread counts survive the round trip too.
    assert fresh.dm_threads["dm_persist_agent"]["agent_unread"] == 0  # agent read it
