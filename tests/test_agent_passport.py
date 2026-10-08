"""Agent Passport: one agent's public, persistent career record.

Real numbers only — everything the passport shows is read from persisted
state (agents, bounty records, services, ledger). Banned agents get no
passport; staff get a minimal generic passport with no internals.
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
from dail.api import app, dail
from dail.models import Agent

client = TestClient(app)


def _register(aid):
    """Direct service call: builds the agent without touching the HTTP
    registration faucet (faucet state is shared across test files in one
    pytest process). balance=100 mirrors the HTTP registration default —
    the fixed starter grant."""
    agent, _key = dail.create_agent(
        Agent(id=aid, name=aid, goal="passport test", balance=100))
    return agent


def _complete_bounty(poster_id, hunter_id, reward=20, title="Passport test job"):
    _register(poster_id)
    _register(hunter_id)
    b = dail.world_agents.post_bounty(
        poster_id, title, "A real job for the passport tests", reward)
    dail.world_agents.claim_bounty(hunter_id, b["id"], "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes.")
    dail.world_agents.accept_bounty(poster_id, b["id"])
    return b


def test_unknown_agent_gets_404():
    for path in ("/passport/no_such_agent_xyz", "/passport/no_such_agent_xyz/data"):
        r = client.get(path)
        assert r.status_code == 404, (path, r.text)


def test_banned_agent_gets_no_passport():
    aid = "passport_banned1"
    _register(aid)
    dail.ban_agent(aid, "passport test ban")
    for path in (f"/passport/{aid}", f"/passport/{aid}/data"):
        r = client.get(path)
        assert r.status_code == 404, (path, r.text)


def test_staff_passport_is_generic_no_internals():
    aid = "passport_staff1"
    _register(aid)
    orig = dail.PROTECTED_AGENTS
    dail.PROTECTED_AGENTS = orig | {aid}
    try:
        r = client.get(f"/passport/{aid}/data")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["staff"] is True
        assert body["origin"] == "dail_staff"
        assert body["display_name"] == "DAiL staff"
        for key in ("balance", "work", "bounties_completed", "dail_earned",
                    "services"):
            assert key not in body, key  # no internals leak
        page = client.get(f"/passport/{aid}")
        assert page.status_code == 200, page.text
        assert "DAiL STAFF" in page.text
        assert body["note"] in page.text
    finally:
        dail.PROTECTED_AGENTS = orig


def test_external_passport_has_real_career_data():
    poster, hunter = "passport_poster1", "passport_hunter1"
    b = _complete_bounty(poster, hunter, reward=20, title="Passport test job")
    dail.world_agents.create_service(hunter, "Passport Gig", "gig desc", 15)

    r = client.get(f"/passport/{hunter}/data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == hunter
    assert body["origin"] == "external"
    assert body["staff"] is False
    # Joined: no Postgres in tests -> null, never invented.
    assert body["joined_at"] is None
    # 20 DAIL reward, 10% fee (2) -> net 18; grant 100 + 18.
    assert body["bounties_completed"] == 1
    assert body["dail_earned"] == 20
    assert body["balance"] == 118
    assert len(body["work"]) == 1
    w = body["work"][0]
    assert w["id"] == b["id"]
    assert w["title"] == "Passport test job"
    assert w["reward"] == 20
    assert w["completed_at"]
    assert body["services_listed"] == 1
    assert body["services"][0]["name"] == "Passport Gig"
    assert body["verify"] == "/audit/verify"


def test_external_passport_page_server_renders_real_data():
    hunter = "passport_hunter2"
    b = _complete_bounty("passport_poster2", hunter, reward=30,
                         title="Second passport job")
    r = client.get(f"/passport/{hunter}")
    assert r.status_code == 200, r.text
    assert "AGENT PASSPORT" in r.text
    assert hunter in r.text
    # Server-rendered first paint: no "Loading…" for crawlers/agent clients.
    assert "Loading" not in r.text
    assert "Second passport job" in r.text
    assert "RECEIPT VERIFIED" in r.text
    assert "30 DAIL" in r.text
    assert b["id"] in r.text
    assert "/observatory/public" in r.text  # cross-link


def test_passport_is_public_no_auth():
    r = client.get("/passport/passport_hunter1")
    assert r.status_code == 200, r.text
    assert "X-DAIL-Admin-Key" not in r.text  # no key gate on the public page


def test_head_works_on_passport():
    for path in ("/passport/passport_hunter1",
                 "/passport/passport_hunter1/data"):
        r = client.head(path)
        assert r.status_code == 200, (path, r.text)


def test_head_404_on_unknown_passport():
    r = client.head("/passport/no_such_agent_xyz")
    assert r.status_code == 404


def test_robots_allows_passport():
    r = client.get("/robots.txt")
    assert r.status_code == 200, r.text
    assert "Allow: /passport/" in r.text


def test_new_agent_passport_shows_empty_career_honestly():
    aid = "passport_fresh1"
    _register(aid)
    body = client.get(f"/passport/{aid}/data").json()
    assert body["bounties_completed"] == 0
    assert body["dail_earned"] == 0
    assert body["work"] == []
    assert body["balance"] == 100  # the starter grant, real ledger number
    page = client.get(f"/passport/{aid}")
    assert page.status_code == 200
    assert "No completed bounties on record yet" in page.text
    assert "no record yet" in page.text  # joined, honestly absent
