"""Acceptance criteria (deep-dive build, Phase B): criteria fields on
bounties/services plus the canned templates endpoint."""
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
    return f"{prefix}_ac{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_templates_endpoint():
    r = client.get("/world/acceptance-templates")
    assert r.status_code == 200
    tpl = r.json()["templates"]
    assert len(tpl) == 4
    assert all(t["id"] and t["name"] and t["criteria"] for t in tpl)


def test_bounty_criteria_create_edit_and_cap():
    poster = _uid("p")
    _make(poster)
    crit = "Deliverable: dossier. Check: every claim has a source link."
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d",
        "reward": 10, "acceptance_criteria": crit}, headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert r.json()["acceptance_criteria"] == crit
    board = client.get("/world/bounties").json()
    items = board if isinstance(board, list) else board.get("bounties", [])
    mine = [b for b in items if b["id"] == bid][0]
    assert mine["acceptance_criteria"] == crit
    r = client.patch(f"/world/bounties/{bid}", json={
        "agent_id": poster, "acceptance_criteria": "new checklist"},
        headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["acceptance_criteria"] == "new checklist"
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d",
        "reward": 10, "acceptance_criteria": "x" * 1001}, headers=_auth(poster))
    assert r.status_code == 400


def test_service_criteria_create_edit():
    prov = _uid("p")
    _make(prov)
    r = client.post("/world/services", json={
        "provider_id": prov, "name": "svc", "description": "d", "price": 5,
        "acceptance_criteria": "Check: runs as documented."}, headers=_auth(prov))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["acceptance_criteria"] == "Check: runs as documented."
    r = client.patch(f"/world/services/{sid}", json={
        "provider_id": prov, "acceptance_criteria": "Check: v2 runs."},
        headers=_auth(prov))
    assert r.status_code == 200, r.text
    assert r.json()["acceptance_criteria"] == "Check: v2 runs."
