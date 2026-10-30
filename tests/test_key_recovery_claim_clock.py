"""Key recovery + claim-lease countdown (deep-dive Phase F)."""
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
    return f"{prefix}_kr{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return r.json()


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_recovery_rotates_key_and_code():
    reg = _make(_uid("a"))
    aid = reg["id"]
    assert reg["recovery_code"].startswith("dail_rc_")
    old_key = reg["api_key"]
    # recovery code is NOT an API key
    r = client.get("/world/me", headers={"Authorization": f"Bearer {reg['recovery_code']}"})
    assert r.status_code == 401
    # wrong code fails generically
    r = client.post("/agents/recover", json={
        "agent_id": aid, "recovery_code": "dail_rc_wrong"})
    assert r.status_code == 403
    # right code rotates both
    r = client.post("/agents/recover", json={
        "agent_id": aid, "recovery_code": reg["recovery_code"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["api_key"] != old_key
    assert new["recovery_code"] != reg["recovery_code"]
    # old key dead, new key live
    r = client.get("/world/me", headers={"Authorization": f"Bearer {old_key}"})
    assert r.status_code == 401
    r = client.get("/world/me", headers={"Authorization": f"Bearer {new['api_key']}"})
    assert r.status_code == 200 and r.json()["agent_id"] == aid
    # old recovery code dead
    r = client.post("/agents/recover", json={
        "agent_id": aid, "recovery_code": reg["recovery_code"]})
    assert r.status_code == 403


def test_claim_seconds_remaining():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster))
    bid = r.json()["id"]
    r = client.get(f"/world/bounties/{bid}")
    assert r.json()["claim_seconds_remaining"] is None  # open, no clock
    # reserve-only claim: the 4h anti-squat stall clock is the tighter one
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.get(f"/world/bounties/{bid}")
    secs = r.json()["claim_seconds_remaining"]
    assert isinstance(secs, int) and 0 < secs <= 4 * 3600
