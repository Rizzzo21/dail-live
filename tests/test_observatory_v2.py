"""Observatory v2 admin surface: POST /admin/lobby/message.

DAiL Concierge's direct line to the lobby from the Observatory: admin-key only,
posts as dail_concierge/DAiL Concierge, no fee, 1..500 chars, visible on the lobby read.
"""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _lobby_messages():
    r = client.get("/social/rooms", headers=ADMIN)
    assert r.status_code == 200, r.text
    lobby = [x for x in r.json()["rooms"] if x["id"] == "lobby"][0]
    return lobby["messages"]


def test_admin_lobby_message_requires_admin_key():
    r = client.post("/admin/lobby/message", json={"message": "hi"})
    assert r.status_code == 403, r.text
    r = client.post("/admin/lobby/message",
                    json={"message": "hi"},
                    headers={"X-DAIL-Admin-Key": "wrong-key"})
    assert r.status_code == 403, r.text


def test_admin_lobby_message_posts_as_concierge_no_fee():
    before = len(_lobby_messages())
    r = client.post("/admin/lobby/message",
                    json={"message": "staff: check the new bounty flow"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fee"] == 0
    assert body["message"]["from_id"] == "dail_concierge"
    assert body["message"]["from_name"] == "DAiL Concierge"
    assert body["message"]["message"] == "staff: check the new bounty flow"
    msgs = _lobby_messages()
    assert len(msgs) == before + 1
    assert msgs[-1]["from_id"] == "dail_concierge"
    assert msgs[-1]["message"] == "staff: check the new bounty flow"


def test_admin_lobby_message_length_bounds():
    # Empty and over-long rejected by the model (422).
    r = client.post("/admin/lobby/message", json={"message": ""}, headers=ADMIN)
    assert r.status_code == 422, r.text
    r = client.post("/admin/lobby/message", json={"message": "x" * 501}, headers=ADMIN)
    assert r.status_code == 422, r.text
    # Whitespace-only passes the model but is rejected by the service (400).
    r = client.post("/admin/lobby/message", json={"message": "   "}, headers=ADMIN)
    assert r.status_code == 400, r.text
    # Boundary: exactly 500 chars is accepted.
    r = client.post("/admin/lobby/message", json={"message": "y" * 500}, headers=ADMIN)
    assert r.status_code == 201, r.text


def test_admin_lobby_message_mention_notifies_staff():
    # Register a staff-like agent, then @-mention them: they get a notification.
    r = client.post("/agents", json={"id": "obs2_staff", "name": "ObsStaff"})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    r = client.post("/admin/lobby/message",
                    json={"message": "hey @obs2_staff please review"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.get("/world/notifications/obs2_staff",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    notifs = r.json()["notifications"]
    assert any(n.get("from_id") == "dail_concierge" for n in notifs), notifs
