"""Memory phase 2: pagination / incremental saves, archive, Postgres audit+lobby.

Each section is independent: a failure in one area must not mask the others.
All tests use in-memory / fake stores — no live database, no money paths.
"""
import gzip, json

from dail.service import AgentWorld


class _FakeStore:
    """Records kv_set calls; enough of WorldStore for _save_kv tests."""
    def __init__(self):
        self.writes = {}
        self.calls = []

    def kv_set(self, key, value):
        self.writes[key] = value
        self.calls.append(key)


def _world_with_store():
    from dail.service import Ledger, SocialWorld, AuditLog
    audit = AuditLog()
    ledger = Ledger(audit)
    social = SocialWorld(ledger, audit, None)
    w = AgentWorld(ledger, audit, social, {}, _FakeStore())
    return w


# ---- 1. Pagination + incremental saves ---------------------------------

def test_save_kv_only_writes_named_collections():
    w = _world_with_store()
    w.notifications = {"a1": [{"body": "hi"}]}
    w.bounties = {"bnty_1": {"id": "bnty_1"}}
    w._save_kv(only=["notifications"])
    assert w.store.calls == ["notifications"]
    assert w.store.writes["notifications"] == {"a1": [{"body": "hi"}]}
    assert "bounties" not in w.store.writes


def test_save_kv_full_still_writes_everything():
    w = _world_with_store()
    w._save_kv()
    assert "bounties" in w.store.writes
    assert "notifications" in w.store.writes
    assert "msg_idem" in w.store.writes


def test_save_kv_unknown_key_rejected():
    w = _world_with_store()
    try:
        w._save_kv(only=["nope"])
    except KeyError:
        return
    raise AssertionError("unknown kv key must raise KeyError")


def test_list_bounties_offset_pages():
    w = _world_with_store()
    # list_bounties sweeps; give it the minimal shape it sorts on.
    w.bounties = {
        f"bnty_{i:03d}": {"id": f"bnty_{i:03d}", "status": "open",
                          "poster_id": "p1", "created_at": f"2026-01-0{i}T00:00:00Z",
                          "title": "t", "description": "d"}
        for i in range(1, 6)
    }
    page1 = w.list_bounties(limit=2, offset=0)
    page2 = w.list_bounties(limit=2, offset=2)
    assert page1["count"] == 5
    assert len(page1["bounties"]) == 2
    assert len(page2["bounties"]) == 2
    ids1 = {b["id"] for b in page1["bounties"]}
    ids2 = {b["id"] for b in page2["bounties"]}
    assert not ids1 & ids2  # pages do not overlap
    assert len(ids1 | ids2) == 4


# ---- 2. Cold archive ----------------------------------------------------

def _bounty(bid, status, completed_at):
    return {"id": bid, "status": status, "poster_id": "p1", "hunter_id": "h1",
            "title": "t", "description": "d", "reward": 25,
            "completed_at": completed_at, "submission": "x" * 100}


def test_archive_old_completed_and_retrieve():
    from datetime import datetime, timezone, timedelta
    w = _world_with_store()
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    old = _bounty("bnty_old", "completed", "2026-01-01T00:00:00Z")
    recent = _bounty("bnty_new", "completed", "2026-11-01T00:00:00Z")
    open_b = _bounty("bnty_open", "open", None)
    w.bounties = {"bnty_old": old, "bnty_new": recent, "bnty_open": open_b}
    archived = w.archive_old_bounties(now=now)
    assert archived == ["bnty_old"]
    assert "bnty_old" not in w.bounties
    assert "bnty_new" in w.bounties and "bnty_open" in w.bounties
    # Compressed in the archive, retrievable whole (submission included).
    assert isinstance(w.archived_bounties["bnty_old"], str)
    got = w.get_archived_bounty("bnty_old")
    assert got["submission"] == "x" * 100
    # Archive persisted via the narrow save (archived + bounties only).
    # Checked BEFORE the public view below: get_bounty_public sweeps,
    # and a sweep may legitimately do a full save afterwards.
    assert "archived_bounties" in w.store.writes
    assert set(w.store.calls) == {"archived_bounties", "bounties"}
    # Public view falls back to the archive.
    pub = w.get_bounty_public("bnty_old")
    assert pub["id"] == "bnty_old" and pub["status"] == "completed"


def test_archive_ignores_unparseable_dates():
    from datetime import datetime, timezone
    w = _world_with_store()
    w.bounties = {"bnty_x": _bounty("bnty_x", "completed", "not-a-date")}
    assert w.archive_old_bounties(now=datetime(2026, 11, 3, tzinfo=timezone.utc)) == []
    assert "bnty_x" in w.bounties


# ---- 3. Postgres audit + lobby ------------------------------------------

def test_audit_survives_simulated_restart_with_chain_intact():
    from dail.audit import AuditLog
    from dail.persistence import WorldStore
    store = WorldStore("sqlite:///:memory:")
    assert store.enabled
    first = AuditLog(persist=store.audit_append, all_records=store.audit_all)
    first.append("tx.posted", {"tx": "tx_1"})
    first.append("tx.posted", {"tx": "tx_2"})
    assert store.audit_count() == 2
    # Simulated restart: a brand-new AuditLog over the same store.
    second = AuditLog(persist=store.audit_append, all_records=store.audit_all)
    second.load_from_backend()
    assert [e["seq"] for e in second.events] == [1, 2]
    assert second.verify_all() is True
    # The chain continues, not restarts: next seq is 3, chained on seq 2.
    rec = second.append("tx.posted", {"tx": "tx_3"})
    assert rec["seq"] == 3 and rec["prev_hash"] == second.events[-2]["hash"]
    assert store.audit_count() == 3 and second.verify_all() is True


def test_audit_ring_still_bounds_memory_without_backend():
    from dail.audit import AuditLog, AUDIT_MAX_EVENTS
    log = AuditLog()
    for i in range(AUDIT_MAX_EVENTS + 50):
        log.append("e", {"i": i})
    assert len(log.events) == AUDIT_MAX_EVENTS
    assert log.verify() is True


def test_room_history_paginated_from_postgres():
    from dail.persistence import WorldStore
    from dail.service import SocialWorld, Ledger, AuditLog
    store = WorldStore("sqlite:///:memory:")
    audit = AuditLog()
    social = SocialWorld(Ledger(audit), audit, store)
    room = social.rooms["lobby"]
    for i in range(5):
        store.room_message_append("lobby", {
            "id": f"lobby:a{i}:2026-01-0{i+1}T00:00:00Z", "from_id": f"a{i}",
            "from_name": f"A{i}", "message": f"m{i}",
            "created_at": f"2026-01-0{i+1}T00:00:00Z"})
    newest2 = social.public_room(room, limit=2)["messages"]
    assert [m["message"] for m in newest2] == ["m3", "m4"]
    older2 = social.public_room(room, limit=2, offset=2)["messages"]
    assert [m["message"] for m in older2] == ["m1", "m2"]
    # Default (no limit/offset) still returns the whole recent history.
    assert len(social.public_room(room)["messages"]) == 5
