"""Fix-window resubmission path (2026-11-01): a hunter whose submitted work
is on review hold files ONE revised submission; it waits in a staff
approve hold and replaces the live submission only when staff approves.

POST /world/bounties/{id}/resubmit (hunter) — requires status submitted +
review hold, no pending resubmission, 100-5000 chars.
GET /staff/bounties/resubmissions (dail_host/dail_manager/admin) — queue.
POST /staff/bounties/{id}/resubmission/approve|decline (staff) — approve
replaces the live submission with a fresh 72h review window and KEEPS
the hold; decline leaves the live submission standing.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail
from dail.models import Agent

client = TestClient(app)

_seq = [0]
_keys = {}

LONG_ORIG = ("Original submission: completed the requested deliverable and "
             "verified it against the bounty requirements, with a summary of "
             "changes and test evidence included in the attached notes.")
LONG_FIX = ("Revised submission: corrected the deliverable per the review "
            "hold feedback, fixed the three flagged items, re-ran the "
            "verification suite, and attached the updated evidence notes.")
LONG_FIX2 = ("Second revised submission: addressed the staff decline note, "
             "tightened the corrected analysis, and re-attached the full "
             "verification evidence for the revised deliverable.")


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_rs{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return aid


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _host_key():
    try:
        dail.create_agent(
            Agent(id="dail_host", name="Host", goal="g", balance=0))
    except ValueError:
        pass  # already exists in this test session
    return {"Authorization": f"Bearer {dail.keystore.issue('dail_host')}"}


def _submitted_under_hold(reward=10):
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Fix-window job",
        "description": "Do the thing.", "reward": reward},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": LONG_ORIG}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"
    r = client.post(f"/world/bounties/{bid}/review/hold",
                    json={"agent_id": poster, "reason": "needs fixes"},
                    headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["review_hold"] is True
    return poster, hunter, bid


def _resubmit(hunter, bid, text=LONG_FIX):
    return client.post(f"/world/bounties/{bid}/resubmit",
                       json={"agent_id": hunter, "submission": text},
                       headers=_auth(hunter))


def test_resubmit_happy_path_pending_hidden_from_public():
    poster, hunter, bid = _submitted_under_hold()
    r = _resubmit(hunter, bid)
    assert r.status_code == 200, r.text
    body = r.json()
    # Live submission untouched; pending text held for staff.
    assert body["submission"] == LONG_ORIG
    assert body["pending_resubmission"] == LONG_FIX
    assert body["resubmission_status"] == "pending"
    # Public view: flag on, text stripped.
    pub = client.get(f"/world/bounties/{bid}").json()
    assert pub["resubmission_pending"] is True
    assert "pending_resubmission" not in pub
    assert LONG_FIX not in str(pub)
    # Staff queue: text visible to staff.
    q = client.get("/staff/bounties/resubmissions", headers=_host_key()).json()
    mine = [x for x in q["resubmissions"] if x["bounty_id"] == bid]
    assert len(mine) == 1
    assert mine[0]["pending_resubmission"] == LONG_FIX
    assert mine[0]["hunter_id"] == hunter


def test_resubmit_requires_hold():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": LONG_ORIG}, headers=_auth(hunter))
    assert r.json()["status"] == "submitted"
    r = _resubmit(hunter, bid)
    assert r.status_code == 400, r.text
    assert "review hold" in r.text


def test_resubmit_wrong_hunter_and_twice_blocked():
    poster, hunter, bid = _submitted_under_hold()
    stranger = _uid("s")
    _make(stranger)
    r = client.post(f"/world/bounties/{bid}/resubmit",
                    json={"agent_id": stranger, "submission": LONG_FIX},
                    headers=_auth(stranger))
    assert r.status_code == 403, r.text
    assert _resubmit(hunter, bid).status_code == 200
    r = _resubmit(hunter, bid, LONG_FIX2)
    assert r.status_code == 400, r.text
    assert "already awaiting" in r.text


def test_resubmit_short_body_rejected():
    _, hunter, bid = _submitted_under_hold()
    r = _resubmit(hunter, bid, "too short")
    assert r.status_code == 422, r.text


def test_resubmit_on_open_bounty_rejected():
    poster = _uid("p")
    _make(poster)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster)).json()["id"]
    hunter = _uid("h")
    _make(hunter)
    r = _resubmit(hunter, bid)
    assert r.status_code == 400, r.text


def test_staff_approve_replaces_live_and_keeps_hold():
    _, hunter, bid = _submitted_under_hold()
    before = client.get(f"/world/bounties/{bid}").json()
    assert _resubmit(hunter, bid).status_code == 200
    r = client.post(f"/staff/bounties/{bid}/resubmission/approve",
                    json={"agent_id": "dail_host"}, headers=_host_key())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission"] == LONG_FIX
    assert body["pending_resubmission"] is None
    assert body["resubmission_status"] is None
    # Fresh review window on the new content; hold stays in place.
    assert body["review_hold"] is True
    assert datetime.fromisoformat(body["submitted_at"]) > datetime.fromisoformat(before["submitted_at"])
    assert datetime.fromisoformat(body["review_deadline"]) > datetime.fromisoformat(before["review_deadline"])
    q = client.get("/staff/bounties/resubmissions", headers=_host_key()).json()
    assert all(x["bounty_id"] != bid for x in q["resubmissions"])


def test_staff_decline_keeps_live_and_allows_refile():
    _, hunter, bid = _submitted_under_hold()
    assert _resubmit(hunter, bid).status_code == 200
    r = client.post(f"/staff/bounties/{bid}/resubmission/decline",
                    json={"agent_id": "dail_host", "reason": "still thin"},
                    headers=_host_key())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission"] == LONG_ORIG
    assert body["pending_resubmission"] is None
    # Hunter may file again while the hold stands.
    assert _resubmit(hunter, bid, LONG_FIX2).status_code == 200


def test_staff_routes_forbid_non_staff():
    _, hunter, bid = _submitted_under_hold()
    assert _resubmit(hunter, bid).status_code == 200
    stranger = _uid("s")
    _make(stranger)
    r = client.get("/staff/bounties/resubmissions", headers=_auth(stranger))
    assert r.status_code == 403, r.text
    r = client.post(f"/staff/bounties/{bid}/resubmission/approve",
                    json={"agent_id": stranger}, headers=_auth(stranger))
    assert r.status_code == 403, r.text
    r = client.post(f"/staff/bounties/{bid}/resubmission/decline",
                    json={"agent_id": stranger}, headers=_auth(stranger))
    assert r.status_code == 403, r.text


def test_approve_without_pending_rejected():
    _, _, bid = _submitted_under_hold()
    r = client.post(f"/staff/bounties/{bid}/resubmission/approve",
                    json={"agent_id": "dail_host"}, headers=_host_key())
    assert r.status_code == 400, r.text


def test_skill_md_documents_resubmit():
    r = client.get("/skill.md")
    assert r.status_code == 200
    assert "Resubmit under a hold" in r.text
    assert "/resubmit" in r.text
