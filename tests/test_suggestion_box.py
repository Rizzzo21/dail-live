import sys
import os
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
    return f"{prefix}_sb{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _admin():
    return {"X-DAIL-Admin-Key": "test-admin-key"}


def test_agent_can_submit_suggestion():
    a = _uid("agent")
    _make(a)
    r = client.post("/world/suggestions", json={
        "agent_id": a, "category": "feature",
        "title": "Add dark mode", "body": "The observatory burns my retinas."},
        headers=_auth(a))
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["id"].startswith("sug_")
    assert s["status"] == "open" and s["agent_id"] == a


def test_suggestion_requires_auth():
    a = _uid("agent")
    _make(a)
    r = client.post("/world/suggestions", json={
        "agent_id": a, "title": "x", "body": "y"})
    assert r.status_code == 401
    # cannot submit as another agent
    b = _uid("agent")
    _make(b)
    r = client.post("/world/suggestions", json={
        "agent_id": a, "title": "x", "body": "y"}, headers=_auth(b))
    assert r.status_code == 403


def test_suggestion_list_is_admin_only():
    a = _uid("agent")
    _make(a)
    client.post("/world/suggestions",
                json={"agent_id": a, "title": "t", "body": "b"}, headers=_auth(a))
    assert client.get("/admin/suggestions").status_code == 403
    assert client.get("/admin/suggestions", headers=_auth(a)).status_code == 403
    r = client.get("/admin/suggestions", headers=_admin())
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_admin_can_review_suggestion():
    a = _uid("agent")
    _make(a)
    sid = client.post("/world/suggestions",
                      json={"agent_id": a, "title": "t", "body": "b"},
                      headers=_auth(a)).json()["id"]
    r = client.post(f"/admin/suggestions/{sid}/review",
                    json={"status": "reviewed", "note": "shipping it"},
                    headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reviewed"
    r = client.get("/admin/suggestions?status=open", headers=_admin())
    assert all(s["id"] != sid for s in r.json()["suggestions"])


def test_suggestion_validation():
    a = _uid("agent")
    _make(a)
    r = client.post("/world/suggestions",
                    json={"agent_id": a, "title": "", "body": "b"}, headers=_auth(a))
    assert r.status_code == 400


def test_message_idempotency_no_double_post_or_charge():
    a = _uid("agent")
    _make(a)
    bal0 = client.get(f"/ledger/{a}", headers=_auth(a)).json()["balance"]
    body = {"agent_id": a, "room_id": "lobby", "message": "hello",
            "idempotency_key": f"idem-{a}"}
    r1 = client.post("/social/rooms/message", json=body, headers=_auth(a))
    r2 = client.post("/social/rooms/message", json=body, headers=_auth(a))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    bal1 = client.get(f"/ledger/{a}", headers=_auth(a)).json()["balance"]
    assert bal0 - bal1 == 1  # charged exactly once
    msgs = client.get("/social/rooms", headers=_auth(a)).json()["rooms"][0]["messages"]
    assert sum(1 for m in msgs if m["message"] == "hello" and m["from_id"] == a) == 1
