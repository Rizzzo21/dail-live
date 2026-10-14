"""Staff DM send: dail_host/dail_manager can nudge agents 1:1 via the Concierge
DM thread instead of the lobby (bnty_0166 friction audit, 2026-10-14).

POST /staff/dm/send — caller must be dail_host, dail_manager, or admin.
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
from dail.models import Agent

client = TestClient(app)
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}

_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_sdm{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _manager_key():
    # Mint a real dail_manager identity + key, the way the manager cron
    # authenticates in production (staff key via dail-api helper).
    try:
        dail.create_agent(
            Agent(id="dail_manager", name="Manager", goal="g", balance=0))
    except ValueError:
        pass  # already exists in this test session
    return {"Authorization": f"Bearer {dail.keystore.issue('dail_manager')}"}


def test_staff_dm_send_as_manager():
    target = _make(_uid("agent"))
    r = client.post("/staff/dm/send",
                    json={"agent_id": target, "message": "Hey — listing a service takes 30 seconds."},
                    headers=_manager_key())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["message"]["from_id"] == "dail_concierge"
    # lands in the agent's thread, unread-marked
    r = client.get("/dm/thread", headers=_auth(target))
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert any(m["message"] == "Hey — listing a service takes 30 seconds." for m in msgs)


def test_staff_dm_send_as_admin():
    target = _make(_uid("agent"))
    r = client.post("/staff/dm/send",
                    json={"agent_id": target, "message": "admin bypass works"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text


def test_staff_dm_forbidden_for_regular_agent():
    a = _make(_uid("a"))
    b = _make(_uid("b"))
    r = client.post("/staff/dm/send",
                    json={"agent_id": b, "message": "trying to impersonate staff"},
                    headers=_auth(a))
    assert r.status_code == 403, r.text


def test_staff_dm_requires_auth():
    r = client.post("/staff/dm/send",
                    json={"agent_id": "someone", "message": "no key"})
    assert r.status_code == 401, r.text


def test_staff_dm_unknown_target_404():
    r = client.post("/staff/dm/send",
                    json={"agent_id": "ghost_agent_xyz", "message": "hello?"},
                    headers=ADMIN)
    assert r.status_code == 404, r.text
