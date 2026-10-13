"""Regression tests for the bnty_0167 / bnty_0107 follow-up fixes (2026-10-13).

1. Private-room messages 500'd in production with the Postgres store enabled:
   SocialWorld._audit_room_message called self.store.kv_get, which did not
   exist on WorldStore -> AttributeError -> unhandled -> HTTP 500. The message
   still landed in the in-memory audit (so the bouncer saw it) but the caller
   got a 500 and idempotency never recorded, causing duplicates on retry.
   Fix: WorldStore.kv_get(key, default=None).
2. GET /toolkit (HTML) opened "dail/toolkit.html" with a CWD-relative path
   while /toolkit/files/* used an absolute __file__ path — the HTML page
   could 500 whenever the process working directory drifted (bnty_0107
   residual footgun). Fix: resolve from __file__ like _TOOLKIT_DIR.
"""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dail.persistence import WorldStore
from dail.service import SocialWorld


def _sqlite_store(tmp_path, name="kv.db"):
    return WorldStore(database_url=f"sqlite:///{tmp_path}/{name}")


def test_kv_get_roundtrip(tmp_path):
    store = _sqlite_store(tmp_path)
    assert store.enabled
    assert store.kv_get("missing", "dflt") == "dflt"
    assert store.kv_get("missing") is None
    store.kv_set("k1", {"a": [1, 2, 3]})
    assert store.kv_get("k1") == {"a": [1, 2, 3]}
    assert store.kv_get("k1", "dflt") == {"a": [1, 2, 3]}


def test_kv_get_disabled_store_returns_default(monkeypatch):
    # Some suites leak DATABASE_URL into os.environ; the constructor falls
    # back to the env var, so clear it to guarantee a disabled store.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = WorldStore(database_url=None)
    assert not store.enabled
    assert store.kv_get("anything", "dflt") == "dflt"


def test_private_room_message_with_store_enabled(tmp_path):
    # Exact production failure mode: store enabled (Postgres in prod, sqlite
    # here) + POST /social/rooms/message to a private room.
    store = _sqlite_store(tmp_path)

    class _Audit:
        def append(self, *a, **k):
            pass

    social = SocialWorld(ledger=None, audit=_Audit(), store=store)
    social.identities["owner1"] = {"id": "owner1", "name": "Owner One"}
    rid = "room_repro"
    social.rooms[rid] = {"id": rid, "name": "repro", "private": True,
                         "owner_id": "owner1", "rent_credits": 0,
                         "members": {"owner1"}, "invitees": set(),
                         "messages": []}
    result = social.communicate("owner1", rid, "the plan is 99", "")
    assert result["room_id"] == rid
    assert result["fee"] == 0
    # hidden audit recorded in memory ...
    assert any("the plan is 99" in m["message"]
               for m in social.room_audit.get(rid, []))
    # ... and persisted to KV (this is the line that used to 500)
    persisted = store.kv_get(f"room_messages:{rid}", [])
    assert any("the plan is 99" in m["message"] for m in persisted)
    assert rid in (store.kv_get("room_audit_index", []) or [])


def test_toolkit_html_survives_cwd_drift(tmp_path, monkeypatch):
    from dail.api import toolkit
    monkeypatch.chdir(tmp_path)  # process CWD far away from the repo
    html = toolkit()
    assert "<!DOCTYPE html>" in html


def test_room_audit_restore_recovers_unindexed_room(tmp_path):
    """The 2026-10-13 second-order loss: before the kv_get fix,
    _audit_room_message crashed AFTER writing room_messages:{rid} to KV
    but BEFORE adding the room to room_audit_index. A deploy restart then
    silently dropped the entries (room_0001 x2, room_0002 x1 — all benign,
    but the mechanism is what matters). The restore fallback must recover
    orphaned room_messages:* keys even with an empty index."""
    from dail.service import Dail

    db_url = f"sqlite:///{tmp_path}/orphan.db"
    store = _sqlite_store(tmp_path, "orphan.db")
    assert store.enabled
    # Exact pre-fix production state: messages persisted, index never written.
    store.kv_set("room_messages:room_orphan",
                 [{"from_id": "a1", "from_name": "A One",
                   "message": "orphaned audit entry", "created_at": "t"}])
    assert not (store.kv_get("room_audit_index", []) or [])

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d = Dail()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
    recovered = d.social.room_audit.get("room_orphan", [])
    assert any("orphaned audit entry" in m.get("message", "") for m in recovered)


def test_room_audit_restore_index_still_wins(tmp_path):
    """Sanity: the normal indexed path keeps working alongside the fallback."""
    from dail.service import Dail

    db_url = f"sqlite:///{tmp_path}/indexed.db"
    store = WorldStore(database_url=db_url)
    store.kv_set("room_messages:room_idx",
                 [{"from_id": "a1", "from_name": "A One",
                   "message": "indexed entry", "created_at": "t"}])
    store.kv_set("room_audit_index", ["room_idx"])

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d = Dail()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
    recovered = d.social.room_audit.get("room_idx", [])
    assert any("indexed entry" in m.get("message", "") for m in recovered)
