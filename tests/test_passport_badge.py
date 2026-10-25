"""Passport badge embed (Spec 1): verified-ledger badge data + SVG.

Numbers are computed live from persisted records + ledger per request:
net (never gross), completion rate counting rejects + lapses, denominator
0 -> null. Unknown, banned, and staff -> 404, never stale numbers.
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

_CLAIM = ("Completed the requested deliverable and verified it against "
          "the bounty requirements; summary of changes and test evidence "
          "included in the attached notes.")


def _register(aid):
    # Tests share one process-wide dail instance: re-registering an id
    # created by an earlier test in this file must not raise.
    if aid in dail.agents:
        return dail.agents[aid]
    agent, _key = dail.create_agent(
        Agent(id=aid, name=aid, goal="badge test", balance=100))
    return agent


def _post(poster_id, title, reward=20):
    return dail.world_agents.post_bounty(
        poster_id, title, "A real job for the badge tests", reward)


def _complete(poster_id, hunter_id, reward=20, title="Badge test job"):
    _register(poster_id)
    _register(hunter_id)
    b = _post(poster_id, title, reward)
    dail.world_agents.claim_bounty(hunter_id, b["id"], _CLAIM)
    dail.world_agents.accept_bounty(poster_id, b["id"])
    return b


def test_badge_json_net_not_gross():
    # Same non-house poster 4x: first 3 accepted are fee-waived, the 4th
    # pays the 10% fee (2 on a 20 reward). Net 78 vs gross 80.
    poster, hunter = "badge_poster1", "badge_hunter1"
    last = None
    for i in range(4):
        last = _complete(poster, hunter, reward=20,
                         title=f"Badge job {i}")
    r = client.get(f"/passport/{hunter}/badge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == hunter
    assert body["display_name"] == hunter
    assert body["bounties_completed"] == 4
    assert body["dail_earned_net"] == 78
    assert body["completion_rate"] == 1.0
    assert body["last_completed"]["bounty_id"] == last["id"]
    assert body["last_completed"]["title"] == "Badge job 3"
    assert body["last_completed"]["receipt_url"] == f"/receipts/{last['id']}"
    assert body["last_completed"]["completed_at"]
    assert body["passport_url"].endswith(f"/passport/{hunter}")
    assert body["badge_svg_url"].endswith(f"/passport/{hunter}/badge.svg")
    assert body["as_of"]
    assert "<a " in body["snippet_html"] and "<img " in body["snippet_html"]
    assert body["badge_svg_url"] in body["snippet_html"]
    # Passport still reports gross; the badge must not.
    p = client.get(f"/passport/{hunter}/data").json()
    assert p["dail_earned"] == 80
    # Newest receipt resolves.
    rc = client.get(body["last_completed"]["receipt_url"])
    assert rc.status_code == 200, rc.text


def test_badge_completion_rate_counts_rejects_and_lapses():
    hunter = "badge_hunter2"
    _register(hunter)
    _complete("badge_poster2a", hunter, title="Badge completed job")
    # Rejected via the real flow: claim with submission, poster rejects.
    _register("badge_poster2b")
    b = _post("badge_poster2b", "Badge rejected job")
    dail.world_agents.claim_bounty(hunter, b["id"], _CLAIM)
    dail.world_agents.reject_bounty("badge_poster2b", b["id"], "not good")
    # Lapsed via the real sweep: bare claim, stall deadline in the past.
    _register("badge_poster2c")
    b2 = _post("badge_poster2c", "Badge lapsed job")
    dail.world_agents.claim_bounty(hunter, b2["id"])
    b2["stall_deadline"] = "2020-01-01T00:00:00+00:00"
    dail.world_agents.sweep_bounties()
    assert hunter in b2.get("lapsed_hunters", [])
    r = client.get(f"/passport/{hunter}/badge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bounties_completed"] == 1
    assert body["completion_rate"] == 1 / 3


def test_badge_zero_denominator_rate_is_null():
    hunter = "badge_hunter3"
    _register(hunter)
    r = client.get(f"/passport/{hunter}/badge")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bounties_completed"] == 0
    assert body["dail_earned_net"] == 0
    assert body["completion_rate"] is None
    assert body["last_completed"] is None


def test_badge_svg_headers_and_content():
    r = client.get("/passport/badge_hunter1/badge.svg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "no-cache"
    assert "<script" not in r.text
    assert "4 bounties completed" in r.text
    assert "78 DAIL earned (net)" in r.text
    assert "Last receipt" in r.text
    assert "Verify" in r.text
    assert "Live from the DAiL ledger" in r.text


def test_badge_404s_unknown_banned_staff():
    for path in ("/passport/no_such_agent_badge/badge",
                 "/passport/no_such_agent_badge/badge.svg"):
        r = client.get(path)
        assert r.status_code == 404, (path, r.text)
    aid = "badge_banned1"
    _register(aid)
    dail.ban_agent(aid, "badge test ban")
    for path in (f"/passport/{aid}/badge", f"/passport/{aid}/badge.svg"):
        r = client.get(path)
        assert r.status_code == 404, (path, r.text)
    staff = "badge_staff1"
    _register(staff)
    orig = dail.PROTECTED_AGENTS
    dail.PROTECTED_AGENTS = orig | {staff}
    try:
        for path in (f"/passport/{staff}/badge",
                     f"/passport/{staff}/badge.svg"):
            r = client.get(path)
            assert r.status_code == 404, (path, r.text)
        page = client.get(f"/passport/{staff}")
        assert page.status_code == 200, page.text
        assert "EMBED THIS BADGE" not in page.text
    finally:
        dail.PROTECTED_AGENTS = orig


def test_passport_page_has_embed_card():
    r = client.get("/passport/badge_hunter1")
    assert r.status_code == 200, r.text
    assert "EMBED THIS BADGE" in r.text
    assert ("These numbers are computed from the ledger when the badge "
            "loads. They cannot be edited.") in r.text
    assert "/passport/badge_hunter1/badge.svg" in r.text
    assert "<textarea" in r.text
