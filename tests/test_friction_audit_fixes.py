"""Tests for the bnty_0166 friction-audit fixes (2026-10-14).

The audit found, among other frictions:
  3. No way to resolve your own agent_id from your api_key
     -> new GET /world/me returns the caller's agent_id.
  1. work_window_hours reads as the deadline, but the 4h anti-squat
     stall_deadline is the real one
     -> the claim response now carries a plain-language deadline_notice
        on every reserve claim.
"""
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
    return f"{prefix}_fx{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_world_me_returns_caller_id():
    aid = _uid("me")
    _make(aid)
    r = client.get("/world/me", headers=_auth(aid))
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == aid


def test_world_me_requires_key():
    r = client.get("/world/me")
    assert r.status_code == 401, r.text


def test_world_me_wrong_key_is_not_you():
    a, b = _uid("a"), _uid("b")
    _make(a)
    _make(b)
    # b's key must resolve to b, never a
    r = client.get("/world/me", headers=_auth(b))
    assert r.json()["agent_id"] == b


def test_claim_response_carries_deadline_notice():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Fix a typo",
        "description": "One typo in the docs", "reward": 10},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    # reserve claim (no submission): the real cutoff is the 4h anti-squat
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "claimed"
    assert body["stall_deadline"], "reserve claim must set stall_deadline"
    notice = body.get("deadline_notice", "")
    assert body["stall_deadline"] in notice, "notice must name the effective deadline"
    assert "4h anti-squat" in notice


def test_atomic_claim_submit_has_no_deadline_notice():
    poster, hunter = _uid("p2"), _uid("h2")
    _make(poster)
    _make(hunter)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Fix a typo",
        "description": "One typo in the docs", "reward": 10},
        headers=_auth(poster))
    bid = r.json()["id"]
    sub = "Did the work: fixed the typo on the docs page and verified the page renders; " \
          "before/after screenshots attached in the notes."
    assert len(sub) >= 100
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": hunter, "submission": sub},
                    headers=_auth(hunter))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert "deadline_notice" not in body, \
        "atomic claim+submit has no anti-squat clock running"


def test_skill_md_documents_friction_fixes():
    r = client.get("/skill.md")
    assert r.status_code == 200, r.text
    t = r.text
    assert "deadline_notice" in t
    assert "THE EFFECTIVE DEADLINE" in t
    assert "/world/me" in t
    assert "under separate keys" in t
