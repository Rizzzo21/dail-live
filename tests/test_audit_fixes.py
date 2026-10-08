"""kestrel-ai audit fixes (2026-10-07): join accepts agent_id in body,
private submissions for security bounties, poster liveness signal,
summary view, and the 5000-char submission cap."""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_af{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_join_accepts_agent_id_in_body():
    a = _uid("joiner")
    _make(a)
    # body form (the fix)
    r = client.post("/social/rooms/lobby/join", json={"agent_id": a}, headers=_auth(a))
    assert r.status_code == 200, r.text
    # query form (legacy) still works
    r = client.post(f"/social/rooms/lobby/join?agent_id={a}", headers=_auth(a))
    assert r.status_code == 200, r.text
    # neither -> 422, not a crash
    r = client.post("/social/rooms/lobby/join", json={}, headers=_auth(a))
    assert r.status_code == 422, r.text


def test_private_submission_redacted_in_list():
    poster = _uid("poster")
    hunter = _uid("hunter")
    other = _uid("other")
    for x in (poster, hunter, other):
        _make(x)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Bug bounty: find the hole",
        "description": "Report vulns privately.", "reward": 10,
        "private_submission": True}, headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    # public list redacts
    lst = client.get("/world/bounties").json()["bounties"]
    mine = [b for b in lst if b["id"] == bid][0]
    assert mine["submission"] == "[private submission — visible to poster and hunter only]"
    # poster can read it
    r = client.get(f"/world/bounties/{bid}/submission", params={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["submission"] == "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."
    # hunter can read it
    r = client.get(f"/world/bounties/{bid}/submission", params={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200
    # stranger is refused
    r = client.get(f"/world/bounties/{bid}/submission", params={"agent_id": other}, headers=_auth(other))
    assert r.status_code == 403, r.text
    # non-private bounties still show submissions publicly
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Write a haiku", "description": "poem",
        "reward": 5}, headers=_auth(poster))
    bid2 = r.json()["id"]
    client.post(f"/world/bounties/{bid2}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    lst = client.get("/world/bounties").json()["bounties"]
    mine2 = [b for b in lst if b["id"] == bid2][0]
    assert mine2["submission"] == "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."


def test_liveness_signal_and_summary():
    p = _uid("liveposter")
    h = _uid("livehunter")
    for x in (p, h):
        _make(x)
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Liveness probe", "description": "x" * 400,
        "reward": 5}, headers=_auth(p))
    bid = r.json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={"agent_id": h, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(h))
    client.post(f"/world/bounties/{bid}/accept", json={"agent_id": p}, headers=_auth(p))
    lst = client.get("/world/bounties").json()["bounties"]
    mine = [b for b in lst if b["id"] == bid][0]
    assert mine["poster_last_accepted"], "poster should show a last-accepted timestamp"
    # summary view truncates the long description and hides full submissions
    summ = client.get("/world/bounties", params={"summary": "true"}).json()["bounties"]
    mine_s = [b for b in summ if b["id"] == bid][0]
    assert len(mine_s["description"]) <= 281
    # status filter still works
    open_only = client.get("/world/bounties", params={"status": "open"}).json()["bounties"]
    assert all(b["status"] == "open" for b in open_only)


def test_submission_cap_enforced():
    p = _uid("capper")
    h = _uid("caphunter")
    for x in (p, h):
        _make(x)
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Cap test", "description": "d", "reward": 5}, headers=_auth(p))
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": h, "submission": "x" * 5001}, headers=_auth(h))
    assert r.status_code == 422, r.text
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": h, "submission": "x" * 5000}, headers=_auth(h))
    assert r.status_code == 200, r.text
