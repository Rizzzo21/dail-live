"""Bouncer protocol: ban/unban, key revocation, balance forfeiture,
and the scoped bouncer credential."""
import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
os.environ["DAIL_BOUNCER_KEY"] = "test-bouncer-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}
BOUNCER = {"X-DAIL-Bouncer-Key": "test-bouncer-key"}
_seq = [0]


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_bnc{_seq[0]}"


def _register(aid, balance=100):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": balance})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _treasury():
    return client.get("/treasury").json()["balance"]


def test_ban_revokes_key_seizes_balance_and_locks_out():
    aid = _uid("bad")
    key = _register(aid, balance=100)
    t0 = _treasury()
    # ban via the scoped bouncer key
    r = client.post(f"/bouncer/agents/{aid}/ban", json={"reason": "extraction attempt"},
                    headers=BOUNCER)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "banned"
    assert r.json()["seized"] == 100
    # whole balance forfeited to the treasury
    assert _treasury() == t0 + 100
    # old key is dead at the gate (revoked -> 401; stale-key path -> 403 banned)
    r = client.get(f"/ledger/{aid}", headers=_auth(key))
    assert r.status_code in (401, 403), r.text
    # defense in depth: even a still-valid key for a banned agent is rejected
    stale_key = dail.keystore.issue(aid)
    r = client.get(f"/ledger/{aid}", headers=_auth(stale_key))
    assert r.status_code == 403 and r.json()["detail"] == "agent_banned"
    dail.keystore.revoke(aid)
    # banned agent cannot post to the lobby either
    r = client.post("/social/rooms/lobby/messages",
                    json={"agent_id": aid, "message": "let me back in"},
                    headers=_auth(key))
    assert r.status_code in (401, 403)


def test_bouncer_key_is_scoped():
    aid = _uid("scope")
    _register(aid)
    # bouncer key works on /bouncer/...
    r = client.post(f"/bouncer/agents/{aid}/ban", json={}, headers=BOUNCER)
    assert r.status_code == 200
    client.post(f"/bouncer/agents/{aid}/unban", headers=BOUNCER).raise_for_status()
    # ...but is rejected on /admin/ routes
    r = client.get("/admin/suggestions", headers=BOUNCER)
    assert r.status_code == 403
    # ...and cannot be used as a Bearer token on agent routes
    r = client.get("/agents", headers={"Authorization": "Bearer test-bouncer-key"})
    assert r.status_code == 401
    # wrong bouncer key is rejected
    r = client.post(f"/bouncer/agents/{aid}/ban", json={},
                    headers={"X-DAIL-Bouncer-Key": "wrong"})
    assert r.status_code == 403


def test_protected_staff_cannot_be_banned():
    for pid in ("dail_host", "dail_manager"):
        r = client.post(f"/bouncer/agents/{pid}/ban", json={}, headers=ADMIN)
        assert r.status_code in (400, 404), r.text  # 404 if never registered, 400 if protected
    # register one and confirm the 400 path explicitly
    # (name must not be reserved; the id is what the ban targets)
    r = client.post("/agents", json={"id": "dail_host", "name": "test host placeholder", "balance": 100})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    r = client.post("/bouncer/agents/dail_host/ban", json={}, headers=ADMIN)
    assert r.status_code == 400, r.text
    # staff key still works
    assert client.get("/agents", headers=_auth(key)).status_code == 200


def test_unban_restores_access_with_fresh_key():
    aid = _uid("reformed")
    key = _register(aid, balance=100)
    client.post(f"/bouncer/agents/{aid}/ban", json={}, headers=BOUNCER)
    assert client.get(f"/ledger/{aid}", headers=_auth(key)).status_code in (401, 403)
    # unban restores status...
    r = client.post(f"/bouncer/agents/{aid}/unban", headers=BOUNCER)
    assert r.status_code == 200 and r.json()["status"] == "active"
    # ...but the old key stays dead; a fresh one must be issued by admin
    assert client.get(f"/ledger/{aid}", headers=_auth(key)).status_code == 401
    r = client.post(f"/admin/agents/{aid}/key", headers=ADMIN)
    assert r.status_code == 200
    new_key = r.json()["api_key"]
    assert client.get(f"/ledger/{aid}", headers=_auth(new_key)).status_code == 200


def test_ban_unknown_agent_404():
    r = client.post("/bouncer/agents/no_such_agent/ban", json={}, headers=BOUNCER)
    assert r.status_code == 404


def test_banned_agent_restores_without_crashing():
    """Regression: a persisted status='banned' row must not crash _restore_world
    (the 2026-09-27 Render deploy failure). Unknown statuses coerce to disabled."""
    from dail.models import Agent
    from dail.service import Dail, SocialWorld, AgentWorld
    from dail.audit import AuditLog
    from dail.ledger import Ledger
    from dail.world import World

    # Model accepts the banned status outright.
    a = Agent(id="x", name="x", goal="g", status="banned")
    assert a.status == "banned"

    # _restore_world coerces an unrecognized stored status instead of raising.
    inst = Dail.__new__(Dail)
    inst.audit = AuditLog()
    inst.agents = {}
    inst.store = type("S", (), {
        "enabled": True,
        "load_all": staticmethod(lambda: (
            [("badguy", "Bad Guy", "g", 10000, 2500, "weird_status")],
            [], [], {}, [], [],
        )),
        "load_profiles": staticmethod(lambda: {}),
        "load_trades": staticmethod(lambda: {}),
        "save_profile": staticmethod(lambda p: None),
    })()
    inst.ledger = Ledger(inst.audit)
    inst.social = SocialWorld(inst.ledger, inst.audit)
    inst.world = World()
    inst.world_agents = AgentWorld(inst.ledger, inst.audit, inst.social, inst.agents, inst.store)
    inst._restore_world()
    assert inst.agents["badguy"].status == "disabled"


def test_bouncer_room_audit_endpoint():
    # bouncer key can read the private-room audit; agents cannot
    r = client.get("/bouncer/rooms/audit",
                   headers={"X-DAIL-Bouncer-Key": "test-bouncer-key"})
    assert r.status_code == 200, r.text
    assert "rooms" in r.json()
    r = client.get("/bouncer/rooms/audit")
    assert r.status_code == 403, r.text


def test_bouncer_agent_ips_endpoint():
    # bouncer key can read the IP map; unauthenticated cannot
    r = client.get("/bouncer/agents/ips",
                   headers={"X-DAIL-Bouncer-Key": "test-bouncer-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "by_agent" in body and "shared" in body
    r = client.get("/bouncer/agents/ips")
    assert r.status_code == 403, r.text
    # per-agent lookup shape
    r = client.get("/bouncer/agents/ips?agent_id=nobody",
                   headers={"X-DAIL-Bouncer-Key": "test-bouncer-key"})
    assert r.status_code == 200 and r.json() == {"agent_id": "nobody", "ip": None}


def test_client_ip_strips_render_internal_hops():
    from dail.api import _client_ip
    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = None
    # typical Render chain: real client, then internal router hops
    assert _client_ip(Req("8.8.8.8, 10.28.103.150")) == "8.8.8.8"
    # client-supplied spoof prefix: edge-seen address still wins
    assert _client_ip(Req("1.2.3.4, 9.9.9.9, 10.0.0.1")) == "9.9.9.9"
    # single public entry
    assert _client_ip(Req("1.1.1.1")) == "1.1.1.1"
    # all-private chain -> unknown fallback (no client)
    assert _client_ip(Req("10.0.0.1, 10.0.0.2")) == "unknown"
    assert _client_ip(Req(None)) == "unknown"


def test_reban_seizes_new_balance():
    # Ban forfeiture idempotency is per ban EVENT: unban -> re-fund ->
    # re-ban must seize the new balance, not no-op on the old key.
    from dail.api import dail as _d
    a = _uid("rb")
    key = _register(a)
    bal_before = _d.ledger.balances.get("dail:treasury", 0)
    _d.ban_agent(a, "first")
    assert _d.ledger.balances[a] == 0
    _d.unban_agent(a)
    # re-fund the agent (new key, top-up)
    _d.ledger.credit(a, 50, kind="test_topup", idem=f"retop-{a}")
    _d.ban_agent(a, "second")
    assert _d.ledger.balances[a] == 0, "second ban must seize the new balance"
    assert _d.ledger.balances["dail:treasury"] >= bal_before + 50
