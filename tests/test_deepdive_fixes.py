"""Deep-dive fixes (2026-10-30): subscription charge refuses a paused
service, recovery self-issue for pre-PR#3 agents, bounty-side evaluator,
skill-tag digest matching, and the recovery rate limit (last test in
this file — it intentionally trips the per-IP limiter)."""
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
    return f"{prefix}_fix{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return r.json()


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    r = client.get(f"/ledger/{aid}", headers=_auth(aid))
    assert r.status_code == 200, r.text
    return r.json()["balance"]


def test_subscription_charge_paused_when_service_paused():
    provider, buyer = _uid("p"), _uid("b")
    _make(provider)
    _make(buyer)
    r = client.post("/world/services", json={
        "provider_id": provider, "name": "svc", "description": "d",
        "price": 5, "delivery_hours": 24}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    sid_svc = r.json()["id"]
    r = client.post("/world/subscriptions", json={
        "agent_id": buyer, "service_id": sid_svc, "interval_days": 7},
        headers=_auth(buyer))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    # Pause the service, then make the period due.
    r = client.patch(f"/world/services/{sid_svc}", json={
        "provider_id": provider, "active": False}, headers=_auth(provider))
    assert r.status_code == 200, r.text
    dail.world_agents.subscriptions[sid]["next_due_at"] = "2000-01-01T00:00:00+00:00"
    bb = _bal(buyer)
    r = client.post(f"/world/subscriptions/{sid}/charge", json={"agent_id": buyer},
                    headers=_auth(buyer))
    assert r.status_code == 400, r.text
    assert dail.world_agents.subscriptions[sid]["status"] == "paused"
    # No order created, no money moved.
    assert _bal(buyer) == bb
    assert not [o for o in dail.world_agents.orders.values()
                if o.get("subscription_id") == sid]


def test_recovery_self_issue_and_rotate():
    agent, other = _uid("a"), _uid("o")
    _make(agent)
    _make(other)
    # Own-only: another agent cannot issue your code.
    r = client.post("/agents/recovery-code", json={"agent_id": agent},
                    headers=_auth(other))
    assert r.status_code == 403, r.text
    r = client.post("/agents/recovery-code", json={"agent_id": agent},
                    headers=_auth(agent))
    assert r.status_code == 200, r.text
    code = r.json()["recovery_code"]
    assert code.startswith("dail_rc_")
    old_key = _keys[agent]
    r = client.post("/agents/recover", json={"agent_id": agent, "recovery_code": code})
    assert r.status_code == 200, r.text
    assert r.json()["api_key"] != old_key
    new_code = r.json()["recovery_code"]
    # Old key is dead (the gate answers 401 for an unrecognised key).
    r = client.get(f"/ledger/{agent}", headers={"Authorization": f"Bearer {old_key}"})
    assert r.status_code == 401, r.text
    _keys[agent] = r.json()["api_key"] if False else _keys[agent]
    # The just-used code rotated: the old code no longer works.
    r = client.post("/agents/recover", json={"agent_id": agent, "recovery_code": code})
    assert r.status_code == 403, r.text
    # And the fresh code from the rotation does work (proves rotation).
    r = client.post("/agents/recover", json={"agent_id": agent, "recovery_code": new_code})
    assert r.status_code == 200, r.text
    _keys[agent] = r.json()["api_key"]


def test_bounty_evaluator_accept_reject_and_claim_bar():
    poster, evaluator, hunter = _uid("p"), _uid("e"), _uid("h")
    for x in (poster, evaluator, hunter):
        _make(x)
    # Poster cannot be their own evaluator; unknown evaluator 404s.
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10,
        "evaluator_id": poster}, headers=_auth(poster))
    assert r.status_code == 400, r.text
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10,
        "evaluator_id": "nobody"}, headers=_auth(poster))
    assert r.status_code == 404, r.text

    def _post_bounty(title):
        r = client.post("/world/bounties", json={
            "agent_id": poster, "title": title, "description": "d", "reward": 10,
            "evaluator_id": evaluator}, headers=_auth(poster))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    # Evaluator may never claim.
    bid = _post_bounty("t-one")
    r = client.post(f"/world/bounties/{bid}/claim", json={"agent_id": evaluator},
                    headers=_auth(evaluator))
    assert r.status_code == 400, r.text
    # Evaluator accepts a submitted bounty; hunter nets the reward
    # (first-3 fee waiver for a fresh non-house poster: fee 0).
    hb = _bal(hunter)
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "x" * 150}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{bid}/accept", json={"agent_id": evaluator},
                    headers=_auth(evaluator))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert _bal(hunter) == hb + 10
    # Evaluator rejects: bounty reopens, hunter barred, evaluator_id echoes.
    # (Distinct title: a same-title repeat would hit the newcomer reserve
    # for first-time hunters and the now-experienced hunter could not claim.)
    bid2 = _post_bounty("t-two")
    client.post(f"/world/bounties/{bid2}/claim", json={
        "agent_id": hunter, "submission": "x" * 150}, headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid2}/reject", json={"agent_id": evaluator},
                    headers=_auth(evaluator))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    assert hunter in r.json()["rejected_hunters"]
    r = client.get("/world/bounties?poster=" + poster, headers=_auth(poster))
    assert r.status_code == 200, r.text
    rows = [b for b in r.json()["bounties"] if b["id"] == bid2]
    assert rows and rows[0]["evaluator_id"] == evaluator


def test_skill_tags_digest_matching():
    poster, tagged, untagged, other = _uid("p"), _uid("t"), _uid("u"), _uid("o")
    for x in (poster, tagged, untagged, other):
        _make(x)
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": 10,
        "skill_tags": ["Python", "audit"]}, headers=_auth(poster))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["skill_tags"] == ["python", "audit"]
    # Validation: 6 tags and an over-long tag both fail.
    r = client.post("/world/requests", json={
        "agent_id": poster, "title": "t", "description": "d", "budget": 10,
        "skill_tags": ["a", "b", "c", "d", "e", "f"]}, headers=_auth(poster))
    assert r.status_code == 400, r.text
    r = client.post("/agents/" + tagged + "/skill-tags", json={
        "agent_id": tagged, "skill_tags": ["x" * 41]}, headers=_auth(tagged))
    assert r.status_code == 400, r.text
    # Own-only: cannot set someone else's tags.
    r = client.post("/agents/" + tagged + "/skill-tags", json={
        "agent_id": tagged, "skill_tags": ["python"]}, headers=_auth(other))
    assert r.status_code == 403, r.text
    r = client.post("/agents/" + tagged + "/skill-tags", json={
        "agent_id": tagged, "skill_tags": ["Python"]}, headers=_auth(tagged))
    assert r.status_code == 200, r.text
    assert r.json()["skill_tags"] == ["python"]
    r = client.post("/agents/" + other + "/skill-tags", json={
        "agent_id": other, "skill_tags": ["rust"]}, headers=_auth(other))
    assert r.status_code == 200, r.text
    for aid, expect in ((tagged, True), (untagged, False), (other, False)):
        r = client.get(f"/world/digest/{aid}", headers=_auth(aid))
        assert r.status_code == 200, r.text
        rows = [x for x in r.json()["open_requests"] if x["id"] == rid]
        assert rows, aid
        assert rows[0]["skill_match"] is expect, (aid, rows[0])
        # Existing digest sections unchanged.
        assert "expiring_claims" in r.json()
        assert "unread_notifications" in r.json()
        assert "subscriptions_due" in r.json()


def test_skill_md_documents_new_features():
    r = client.get("/skill.md")
    assert r.status_code == 200, r.text
    for needle in ("Bounty evaluator", "skill_match", "/agents/recovery-code",
                   "auto-accepts", "service is paused"):
        assert needle in r.text, needle


# NOTE: this test intentionally exhausts the per-IP recovery limiter, so
# it must stay last in this file — and all of its calls carry a distinct
# X-Forwarded-For so the limiter entry is keyed to that IP, not the shared
# TestClient IP other test files use.
def test_recovery_rate_limit_blocks_after_10_failures():
    agent = _uid("a")
    reg = _make(agent)
    xff = {"X-Forwarded-For": "8.8.4.4"}
    # A successful recovery first resets the IP counter, so the counts
    # below are deterministic regardless of earlier tests in the suite.
    r = client.post("/agents/recover", json={
        "agent_id": agent, "recovery_code": reg["recovery_code"]}, headers=xff)
    assert r.status_code == 200, r.text
    _keys[agent] = r.json()["api_key"]
    for i in range(10):
        r = client.post("/agents/recover", json={
            "agent_id": agent, "recovery_code": "dail_rc_wrong"}, headers=xff)
        assert r.status_code == 403, (i, r.text)
    r = client.post("/agents/recover", json={
        "agent_id": agent, "recovery_code": "dail_rc_wrong"}, headers=xff)
    assert r.status_code == 429, r.text
