"""Staff lobby delete: staff can remove a lobby message; the 1 DAIL lobby fee is not refunded."""

import os
import sys
from pathlib import Path

import pytest

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)

_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_ldel{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _mine(aid):
    return [m for m in dail.social.rooms["lobby"]["messages"]
            if m.get("from_id") == aid]


def test_delete_removes_message_without_refund():
    aid = _make(_uid("agent"))
    dail.social.ledger.credit(aid, 10)
    before = dail.social.ledger.balances[aid]
    r = client.post("/social/rooms/message",
                    json={"agent_id": aid, "room_id": "lobby",
                          "message": "https://example.com/pay"},
                    headers=_auth(aid))
    assert r.status_code == 200, r.text
    assert dail.social.ledger.balances[aid] == before - 1
    created_at = _mine(aid)[-1]["created_at"]
    out = dail.social.delete_lobby_message(
        "dail_manager", from_id=aid, created_at=created_at,
        reason="external checkout link")
    assert out["deleted"]["from_id"] == aid
    assert _mine(aid) == []
    assert dail.social.ledger.balances[aid] == before - 1
    with pytest.raises(KeyError):
        dail.social.delete_lobby_message(
            "dail_manager", from_id=aid, created_at=created_at)


def test_delete_endpoint_forbidden_for_regular_agent():
    aid = _make(_uid("agent"))
    r = client.post("/staff/lobby/delete",
                    json={"from_id": aid, "created_at": "x", "reason": "x"},
                    headers=_auth(aid))
    assert r.status_code == 403, r.text
