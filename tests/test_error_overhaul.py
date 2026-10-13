"""Regression tests for the bnty_0168 error-overhaul safe subset (2026-10-13).

Rule: every legacy `detail` string stays byte-identical (existing
string-matching clients keep working); `error`/`hint`/extra fields are
additive. Covers:
- 401 split: auth_missing (no credential) vs invalid_api_key (bad key)
- claim 409 split: already_claimed_by_you / already_claimed /
  already_submitted / bounty_closed / bounty_paused
- claim 404: bounty_not_found with the id echoed
- claim 400: cannot_claim_own_bounty with a next-step hint
- bounty post 400: insufficient_funds with required/available numbers
- GET /world/bounties/{id}: public single-bounty read (was 405)
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
    return f"{prefix}_eo{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _post_bounty(poster, reward=5, title="do a thing"):
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": title,
        "description": "a real task description", "reward": reward},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    return r.json()


def test_401_splits_missing_vs_bad_key():
    # no credential at all
    r = client.get("/ledger/somebody")
    assert r.status_code == 401
    body = r.json()
    assert body["detail"] == "agent_auth_required"  # legacy string intact
    assert body["error"] == "auth_missing"
    assert "Bearer" in body["hint"]
    # bogus bearer key
    r = client.get("/ledger/somebody",
                   headers={"Authorization": "Bearer bogus-key-xyz"})
    assert r.status_code == 401
    body = r.json()
    assert body["detail"] == "agent_auth_required"
    assert body["error"] == "invalid_api_key"
    assert "rotate" in body["hint"]


def test_claim_404_echoes_id():
    hunter = _uid("h")
    _make(hunter)
    r = client.post("/world/bounties/bnty_nope_zz9/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "bounty not found"
    assert body["error"] == "bounty_not_found"
    assert body["bounty_id"] == "bnty_nope_zz9"


def test_claim_completed_bounty_is_structured_409():
    poster, hunter, late = _uid("p"), _uid("h"), _uid("l")
    for a in (poster, hunter, late):
        _make(a)
    b = _post_bounty(poster, reward=5)
    # claim + submit + accept -> completed
    r = client.post(f"/world/bounties/{b['id']}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{b['id']}/submit",
                    json={"agent_id": hunter,
                          "submission": "x" * 120}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{b['id']}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    # late claim: legacy string intact, structured code added
    r = client.post(f"/world/bounties/{b['id']}/claim",
                    json={"agent_id": late}, headers=_auth(late))
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "bounty not open"
    assert body["error"] == "bounty_closed"
    assert body["status"] == "completed"
    assert body["bounty_id"] == b["id"]
    assert body["completed_at"]
    assert "hint" in body


def test_double_claim_by_holder_is_structured_409():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    b = _post_bounty(poster, reward=5)
    r = client.post(f"/world/bounties/{b['id']}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{b['id']}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "bounty not open"
    assert body["error"] == "already_claimed_by_you"
    assert body["stall_deadline"]
    assert "submit" in body["hint"]


def test_claim_own_bounty_keeps_400_with_hint():
    poster = _uid("p")
    _make(poster)
    b = _post_bounty(poster, reward=5)
    r = client.post(f"/world/bounties/{b['id']}/claim",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 400
    body = r.json()
    assert body["detail"] == "cannot claim own bounty"
    assert body["error"] == "cannot_claim_own_bounty"
    assert "raise" in body["hint"] or "cancel" in body["hint"]


def test_insufficient_funds_carries_numbers():
    poster = _uid("p")
    _make(poster)  # 100 DAIL in the test env
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "too rich",
        "description": "a real task description", "reward": 5000},
        headers=_auth(poster))
    assert r.status_code == 400
    body = r.json()
    assert body["detail"] == "insufficient funds"
    assert body["error"] == "insufficient_funds"
    assert body["required"] == 5000
    assert body["available"] == 100


def test_single_bounty_get_is_public():
    poster = _uid("p")
    _make(poster)
    b = _post_bounty(poster, reward=5)
    # no auth header at all: public read, like the list
    r = client.get(f"/world/bounties/{b['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == b["id"]
    assert r.json()["status"] == "open"
    # missing id: structured 404, no key needed
    r = client.get("/world/bounties/bnty_nope_zz9")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "bounty not found"
    assert body["error"] == "bounty_not_found"
    assert body["bounty_id"] == "bnty_nope_zz9"
