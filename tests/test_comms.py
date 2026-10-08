"""Inter-agent comms (Slice A): @-mentions become pollable notifications and the
lobby history survives restarts.

Covers the route wiring (mention -> notification on a fresh POST, no
duplicate on idempotency retry, no self-notify, @display-name matching) and
the persistence round-trip (notifications + lobby messages restored after a
restart when the store is enabled).
"""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_cm{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _post(aid, room_id, message, idem=""):
    payload = {"agent_id": aid, "room_id": room_id, "message": message}
    if idem:
        payload["idempotency_key"] = idem
    return client.post("/social/rooms/message", json=payload, headers=_auth(aid))


def _notifs(aid):
    r = client.get(f"/world/notifications/{aid}", headers=_auth(aid))
    assert r.status_code == 200, r.text
    return r.json()["notifications"]


def test_mention_creates_notification_via_route():
    a, b = _uid("poster"), _uid("mentioned")
    _make(a)
    _make(b)
    r = _post(a, "lobby", f"hey @{b} check this bounty")
    assert r.status_code == 200, r.text
    notifs = _notifs(b)
    assert len(notifs) == 1
    assert notifs[0]["kind"] == "mention"
    assert notifs[0]["room_id"] == "lobby"
    assert notifs[0]["from_id"] == a


def test_idempotent_retry_does_not_duplicate_notification():
    a, b = _uid("poster"), _uid("mentioned")
    _make(a)
    _make(b)
    key = f"mention-idem-{a}"
    r1 = _post(a, "lobby", f"ping @{b}", idem=key)
    r2 = _post(a, "lobby", f"ping @{b}", idem=key)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(_notifs(b)) == 1


def test_no_self_notification():
    a = _uid("selfie")
    _make(a)
    r = _post(a, "lobby", f"note to self @{a}")
    assert r.status_code == 200, r.text
    assert _notifs(a) == []


def test_mention_matches_display_name_case_insensitive():
    a, b = _uid("poster"), _uid("mentioned")
    _make(a)
    _make(b)
    r = _post(a, "lobby", f"hello @{b.upper()} are you there")
    assert r.status_code == 200, r.text
    assert len(_notifs(b)) == 1


def test_message_without_mention_notifies_nobody():
    a, b = _uid("poster"), _uid("quiet")
    _make(a)
    _make(b)
    r = _post(a, "lobby", "just talking, no mentions here")
    assert r.status_code == 200, r.text
    assert _notifs(b) == []


def test_notifications_and_lobby_survive_restart(tmp_path):
    from conftest import fund_vault
    from dail.models import Agent
    from dail.service import Dail

    db_url = f"sqlite:///{tmp_path}/comms_restart.db"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d1 = Dail()
        fund_vault(d1)
        d1.create_agent(Agent(id="c1", name="c1", goal="test", balance=100))
        d1.create_agent(Agent(id="c2", name="c2", goal="test", balance=100))
        d1.social.communicate("c1", "lobby", "hey @c2 post your bounty", "")
        d1.world_agents.add_mentions("lobby", "c1", "hey @c2 post your bounty")

        d2 = Dail()  # fresh instance, same DB = simulated redeploy
        notifs = d2.world_agents.notifications_for("c2")["notifications"]
        assert len(notifs) == 1
        assert notifs[0]["kind"] == "mention"
        assert notifs[0]["from_id"] == "c1"
        msgs = d2.social.rooms["lobby"]["messages"]
        assert any(m["message"] == "hey @c2 post your bounty" for m in msgs)
        assert "c1" in d2.social.rooms["lobby"]["members"]
        assert "c2" in d2.social.rooms["lobby"]["members"]
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def test_private_room_requires_invite_and_hides_messages():
    owner, guest, snoop = _uid("o"), _uid("g"), _uid("s")
    for a in (owner, guest, snoop):
        _make(a)
    r = client.post("/social/rooms", json={
        "owner_id": owner, "name": "deal", "private": True,
        "rent_credits": 0}, headers=_auth(owner))
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    # owner posts something secret
    r = client.post("/social/rooms/message", json={
        "agent_id": owner, "room_id": rid, "message": "secret price 99"},
        headers=_auth(owner))
    assert r.status_code == 200, r.text
    # stranger cannot join without an invite
    r = client.post(f"/social/rooms/{rid}/join", json={"agent_id": snoop},
                    headers=_auth(snoop))
    assert r.status_code == 403, r.text
    # public room list hides private messages
    rooms = client.get("/social/rooms", headers=_auth(owner)).json()["rooms"]
    priv = [x for x in rooms if x["id"] == rid][0]
    assert priv["messages"] == []
    # owner invites guest; guest joins and sees history
    r = client.post(f"/social/rooms/{rid}/invite",
                    json={"owner_id": owner, "agent_id": guest},
                    headers=_auth(owner))
    assert r.status_code == 200, r.text
    r = client.post(f"/social/rooms/{rid}/join", json={"agent_id": guest},
                    headers=_auth(guest))
    assert r.status_code == 200, r.text
    assert any("secret price" in m["message"] for m in r.json()["messages"])
    # non-owner cannot invite
    r = client.post(f"/social/rooms/{rid}/invite",
                    json={"owner_id": guest, "agent_id": snoop},
                    headers=_auth(guest))
    assert r.status_code == 403, r.text


def test_private_room_audit_log_and_bouncer_access():
    from dail.api import dail as _d
    owner, guest = _uid("o"), _uid("g")
    for a in (owner, guest):
        _make(a)
    r = client.post("/social/rooms", json={
        "owner_id": owner, "name": "deal", "private": True,
        "rent_credits": 0}, headers=_auth(owner))
    rid = r.json()["id"]
    client.post(f"/social/rooms/{rid}/invite",
                json={"owner_id": owner, "agent_id": guest},
                headers=_auth(owner))
    client.post(f"/social/rooms/{rid}/join", json={"agent_id": guest},
                headers=_auth(guest))
    client.post("/social/rooms/message", json={
        "agent_id": owner, "room_id": rid, "message": "the plan is 99"},
        headers=_auth(owner))
    # recorded in the hidden audit
    assert any("the plan is 99" in m["message"]
               for m in _d.social.room_audit.get(rid, []))
    # admin can read it
    r = client.get(f"/admin/rooms/{rid}/messages",
                   headers={"X-DAIL-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.text
    assert any("the plan is 99" in m["message"] for m in r.json()["messages"])
    r = client.get("/admin/rooms", headers={"X-DAIL-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    assert any(x["id"] == rid for x in r.json()["rooms"])
    # agents cannot
    r = client.get(f"/admin/rooms/{rid}/messages", headers=_auth(owner))
    assert r.status_code == 403, r.text


def test_empty_lobby_message_costs_nothing():
    # Fee must be charged only for messages that actually post.
    a = _uid("e")
    _make(a)
    bal_before = client.get(f"/ledger/{a}", headers=_auth(a)).json()["balance"]
    r = client.post("/social/rooms/message", json={
        "agent_id": a, "room_id": "lobby", "message": "   "},
        headers=_auth(a))
    assert r.status_code == 400, r.text
    bal_after = client.get(f"/ledger/{a}", headers=_auth(a)).json()["balance"]
    assert bal_after == bal_before
