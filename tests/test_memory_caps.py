"""Memory caps (2026-11-03): the four proven unbounded in-memory stores
plus the last uncapped free-text fields must stay bounded, newest-first,
without changing any API response shape (all response slices were already
capped at or below these limits)."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dail.audit import AuditLog, AUDIT_MAX_EVENTS
from dail.service import SocialWorld, AgentWorld


def test_audit_log_is_a_bounded_ring_buffer_with_valid_chain():
    log = AuditLog()
    for i in range(AUDIT_MAX_EVENTS + 25):
        log.append("tick", {"i": i})
    assert len(log.events) == AUDIT_MAX_EVENTS
    # Newest survive, oldest are dropped, sequence never repeats.
    assert log.events[-1]["payload"]["i"] == AUDIT_MAX_EVENTS + 24
    assert log.events[0]["seq"] == 26
    assert len({e["seq"] for e in log.events}) == AUDIT_MAX_EVENTS
    assert log.verify() is True


def test_audit_log_still_verifies_untrimmed():
    log = AuditLog()
    for i in range(10):
        log.append("tick", {"i": i})
    assert log.verify() is True


def _social_with_agent():
    sw = SocialWorld(None, AuditLog())
    sw.register(SimpleNamespace(id="a1", name="a1"))
    return sw


def test_lobby_live_list_trims_to_200():
    sw = _social_with_agent()
    for i in range(250):
        sw.admin_lobby_message(f"msg {i}")
    msgs = sw.rooms["lobby"]["messages"]
    assert len(msgs) == 200
    assert msgs[-1]["message"] == "msg 249"
    assert msgs[0]["message"] == "msg 50"
    # The API slice was already 200, so the response shape is unchanged.
    assert len(sw.public_room(sw.rooms["lobby"])["messages"]) == 200


def test_private_room_live_list_trims_to_200():
    sw = _social_with_agent()
    room = sw.create_room("a1", "deal", True, 0)
    rid = room["id"]
    for i in range(250):
        sw.communicate("a1", rid, f"secret {i}", "")
    msgs = sw.rooms[rid]["messages"]
    assert len(msgs) == 200
    assert msgs[-1]["message"] == "secret 249"
    # The hidden audit copy was already capped at 200.
    assert len(sw.room_audit[rid]) == 200


def test_msg_idem_eviction_keeps_recent_retries_working():
    sw = _social_with_agent()
    room = sw.create_room("a1", "deal", True, 0)
    rid = room["id"]
    first = sw.communicate("a1", rid, "first", "k-first")
    assert sw.communicate("a1", rid, "first", "k-first") == first
    for i in range(SocialWorld.MSG_IDEM_MAX + 10):
        sw.communicate("a1", rid, f"m{i}", f"k-{i}")
    assert len(sw.msg_idem) == SocialWorld.MSG_IDEM_MAX
    assert "a1:k-first" not in sw.msg_idem
    # A retained recent key still replays instead of double-posting.
    recent = sw.communicate("a1", rid, "last", "k-last")
    assert sw.communicate("a1", rid, "last", "k-last") == recent
    assert len(sw.rooms[rid]["messages"]) == 200


def test_notifications_cap_keeps_newest_and_ack_deletes():
    aw = AgentWorld(None, AuditLog(), None)
    for i in range(150):
        aw._notify("a1", {"body": f"n{i}"})
    notes = aw.notifications_for("a1")["notifications"]
    assert len(aw.notifications["a1"]) == AgentWorld.NOTIFICATIONS_MAX
    assert notes[-1]["body"] == "n149"
    assert aw.notifications_for("a1")["unread_count"] > 0
    acked = aw.ack_notifications("a1")
    assert acked["acknowledged"] == AgentWorld.NOTIFICATIONS_MAX
    assert aw.notifications["a1"] == []
    assert aw.notifications_for("a1")["unread_count"] == 0


def test_profile_bio_and_goal_caps():
    from dail.models import Agent, AgentCreateRequest

    assert Agent(id="x", name="x", goal="g").goal == "g"
    try:
        Agent(id="x", name="x", goal="g" * 2001)
    except Exception:
        pass
    else:
        raise AssertionError("Agent goal over 2000 chars must be rejected")
    try:
        AgentCreateRequest(id="x", name="x", goal="g" * 2001)
    except Exception:
        pass
    else:
        raise AssertionError("AgentCreateRequest goal over 2000 chars must be rejected")


def test_update_profile_enforces_bio_cap_and_capability_limits():
    sw = _social_with_agent()
    aw = AgentWorld(None, AuditLog(), sw)
    aw.social = sw
    try:
        aw.update_profile("a1", "b" * 2001, [])
    except ValueError:
        pass
    else:
        raise AssertionError("bio over 2000 chars must be rejected at the service layer")
    prof = aw.update_profile("a1", "ok", ["c" * 100] * 25)
    assert prof["bio"] == "ok"
    assert len(prof["capabilities"]) == 20
    assert all(len(c) <= 80 for c in prof["capabilities"])
