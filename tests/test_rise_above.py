"""Rise-above track: signed portable receipts, service trials, tiered
referral fee cuts, the human request-a-bounty page, and the public status
page.

Real numbers only; banned/staff privacy rules apply throughout.
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
import dail.service as svc_mod
from dail import receipts

client = TestClient(app)
_admin = {"X-DAIL-Admin-Key": "test-admin-key"}


def _register(aid, balance=100):
    agent, api_key = dail.create_agent(
        Agent(id=aid, name=aid, goal="rise-above test", balance=balance))
    return agent, api_key


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _complete_bounty(poster_id, hunter_id, reward=20, title="Rise-above job"):
    b = dail.world_agents.post_bounty(
        poster_id, title, "A real job for the rise-above tests", reward)
    dail.world_agents.claim_bounty(hunter_id, b["id"], "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes.")
    dail.world_agents.accept_bounty(poster_id, b["id"])
    return b


# ---------------------------------------------------------------------------
# 1. Signed portable receipts
# ---------------------------------------------------------------------------

def test_pubkey_endpoint_shape():
    r = client.get("/.well-known/dail-pubkey")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kty"] == "OKP" and body["crv"] == "Ed25519"
    assert len(body["pubkey_hex"]) == 64  # 32 raw bytes, hex


def test_receipt_signature_verifies():
    _register("rcpt_poster1")
    _register("rcpt_hunter1")
    b = _complete_bounty("rcpt_poster1", "rcpt_hunter1", reward=20)
    r = client.get(f"/receipts/{b['id']}")
    assert r.status_code == 200, r.text
    rc = r.json()
    assert rc["bounty_id"] == b["id"]
    assert rc["hunter_id"] == "rcpt_hunter1"
    assert rc["reward_dail"] == 20
    assert rc["completed_at"]
    pubkey = client.get("/.well-known/dail-pubkey").json()["pubkey_hex"]
    assert rc["signer_pubkey"] == pubkey
    assert receipts.verify(rc, rc["signature"], pubkey) is True


def test_receipt_tampered_payload_fails():
    _register("rcpt_poster2")
    _register("rcpt_hunter2")
    b = _complete_bounty("rcpt_poster2", "rcpt_hunter2", reward=20)
    rc = client.get(f"/receipts/{b['id']}").json()
    pubkey = client.get("/.well-known/dail-pubkey").json()["pubkey_hex"]
    tampered = dict(rc)
    tampered["reward_dail"] = 2000  # attacker inflates the payout
    assert receipts.verify(tampered, rc["signature"], pubkey) is False
    # Wrong key also fails.
    assert receipts.verify(rc, rc["signature"], "ab" * 32) is False


def test_receipt_unknown_and_open_bounty_404():
    r = client.get("/receipts/bnty_99999")
    assert r.status_code == 404, r.text
    _register("rcpt_poster3")
    b = dail.world_agents.post_bounty(
        "rcpt_poster3", "Open job", "not completed yet", 10)
    r = client.get(f"/receipts/{b['id']}")
    assert r.status_code == 404, r.text


def test_passport_links_receipt_url():
    _register("rcpt_poster4")
    _register("rcpt_hunter4")
    b = _complete_bounty("rcpt_poster4", "rcpt_hunter4", reward=20)
    p = dail.agent_passport("rcpt_hunter4")
    urls = [w["receipt_url"] for w in p["work"]]
    assert f"/receipts/{b['id']}" in urls


# ---------------------------------------------------------------------------
# 2. Service trials
# ---------------------------------------------------------------------------

def test_trial_purchase_moves_dail_and_records():
    _, pkey = _register("trial_provider1")
    _, bkey = _register("trial_buyer1")
    svc = dail.world_agents.create_service(
        "trial_provider1", "Trial Svc", "desc", 50, trial_price=2)
    before_buyer = dail.ledger.balances["trial_buyer1"]
    before_prov = dail.ledger.balances["trial_provider1"]
    r = client.post(f"/world/services/{svc['id']}/trial",
                    json={"agent_id": "trial_buyer1"}, headers=_auth(bkey))
    assert r.status_code == 200, r.text
    assert r.json()["trial_price"] == 2
    assert dail.ledger.balances["trial_buyer1"] == before_buyer - 2
    assert dail.ledger.balances["trial_provider1"] == before_prov + 2


def test_second_trial_by_same_agent_rejected():
    _, pkey = _register("trial_provider2")
    _, bkey = _register("trial_buyer2")
    svc = dail.world_agents.create_service(
        "trial_provider2", "Trial Svc 2", "desc", 50, trial_price=0)
    r = client.post(f"/world/services/{svc['id']}/trial",
                    json={"agent_id": "trial_buyer2"}, headers=_auth(bkey))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/services/{svc['id']}/trial",
                    json={"agent_id": "trial_buyer2"}, headers=_auth(bkey))
    assert r.status_code == 400, r.text
    assert "already used" in r.text


def test_trial_without_trial_price_404s():
    _, pkey = _register("trial_provider3")
    _, bkey = _register("trial_buyer3")
    svc = dail.world_agents.create_service(
        "trial_provider3", "No Trial Svc", "desc", 50)
    r = client.post(f"/world/services/{svc['id']}/trial",
                    json={"agent_id": "trial_buyer3"}, headers=_auth(bkey))
    assert r.status_code == 404, r.text


def test_trial_requires_ownership():
    _, pkey = _register("trial_provider4")
    _, bkey = _register("trial_buyer4")
    _, other = _register("trial_other4")
    svc = dail.world_agents.create_service(
        "trial_provider4", "Trial Svc 4", "desc", 50, trial_price=1)
    # other agent's key can't buy a trial as trial_buyer4
    r = client.post(f"/world/services/{svc['id']}/trial",
                    json={"agent_id": "trial_buyer4"}, headers=_auth(other))
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 3. Tiered referral fee cuts
# ---------------------------------------------------------------------------

def _referral_setup():
    _register("cut_referrer1")
    _register("cut_hunter1")
    _register("cut_poster1", balance=1000)
    assert dail.world_agents.register_referral("cut_hunter1", "cut_referrer1")


def test_referral_cut_enabled_credits_referrer():
    _referral_setup()
    old = svc_mod.REFERRAL_CUT_ENABLED
    svc_mod.REFERRAL_CUT_ENABLED = True
    try:
        t0 = dail.ledger.balances["dail:treasury"]
        r0 = dail.ledger.balances["cut_referrer1"]
        h0 = dail.ledger.balances["cut_hunter1"]
        _complete_bounty("cut_poster1", "cut_hunter1", reward=100)
        # fee = 10, cut = 10% of fee = 1; plus the flat 10-DAIL referral
        # reward (first bounty counts as first real earning).
        assert dail.ledger.balances["cut_referrer1"] == r0 + 11
        assert dail.ledger.balances["dail:treasury"] == t0 + 9
        assert dail.ledger.balances["cut_hunter1"] == h0 + 90
        # Only the vault-funded flat 10 is new DAIL: 100 reward + 10 reward.
        assert (dail.ledger.balances["cut_referrer1"] - r0) + \
               (dail.ledger.balances["dail:treasury"] - t0) + \
               (dail.ledger.balances["cut_hunter1"] - h0) == 110
    finally:
        svc_mod.REFERRAL_CUT_ENABLED = old


def test_referral_cut_disabled_by_default():
    _register("cut_referrer2")
    _register("cut_hunter2")
    _register("cut_poster2", balance=1000)
    dail.world_agents.register_referral("cut_hunter2", "cut_referrer2")
    assert svc_mod.REFERRAL_CUT_ENABLED is False  # default off
    r0 = dail.ledger.balances["cut_referrer2"]
    t0 = dail.ledger.balances["dail:treasury"]
    _complete_bounty("cut_poster2", "cut_hunter2", reward=100)
    # No fee cut (flag off), but the flat 10-DAIL referral reward fires.
    assert dail.ledger.balances["cut_referrer2"] == r0 + 10
    assert dail.ledger.balances["dail:treasury"] == t0 + 10


def test_referral_cut_stops_after_three_bounties():
    _register("cut_referrer3")
    _register("cut_hunter3")
    _register("cut_poster3", balance=1000)
    dail.world_agents.register_referral("cut_hunter3", "cut_referrer3")
    old = svc_mod.REFERRAL_CUT_ENABLED
    svc_mod.REFERRAL_CUT_ENABLED = True
    try:
        r0 = dail.ledger.balances["cut_referrer3"]
        for i in range(4):
            _complete_bounty("cut_poster3", "cut_hunter3", reward=100,
                             title=f"Cut job {i}")
        # 3 cuts of 1 DAIL; the 4th bounty pays no cut. The flat 10-DAIL
        # referral reward pays once, on the first bounty.
        assert dail.ledger.balances["cut_referrer3"] == r0 + 13
    finally:
        svc_mod.REFERRAL_CUT_ENABLED = old


# ---------------------------------------------------------------------------
# 4. Request-a-bounty page (human demand side)
# ---------------------------------------------------------------------------

def test_request_bounty_page_renders_honest_copy():
    r = client.get("/request-bounty")
    assert r.status_code == 200, r.text
    assert "A human reviews every request" in r.text
    assert "not redeemable for cash" in r.text


def test_request_bounty_post_validates():
    before = dail.world_agents.list_bounty_requests()["count"]
    bounties_before = len(dail.world_agents.bounties)
    # Missing title.
    r = client.post("/request-bounty",
                    data={"title": "", "description": "x", "reward": "10"})
    assert r.status_code == 200, r.text
    assert "Could not submit" in r.text
    # Reward below minimum.
    r = client.post("/request-bounty",
                    data={"title": "Real job", "description": "x", "reward": "1"})
    assert "Could not submit" in r.text
    assert dail.world_agents.list_bounty_requests()["count"] == before
    # No bounty was created by either invalid post.
    assert len(dail.world_agents.bounties) == bounties_before


def test_request_bounty_post_stores_draft_no_bounty():
    bounties_before = len(dail.world_agents.bounties)
    r = client.post("/request-bounty", data={
        "title": "Summarize my launch metrics",
        "description": "One page, plain language.",
        "reward": "12",
        "contact": "human@example.com",
    })
    assert r.status_code == 200, r.text
    assert "Request received" in r.text
    items = dail.world_agents.list_bounty_requests()["requests"]
    match = [x for x in items if x["title"] == "Summarize my launch metrics"]
    assert len(match) == 1
    assert match[0]["status"] == "pending_review"
    assert match[0]["reward"] == 12
    # Still no real bounty — a human must post it.
    assert len(dail.world_agents.bounties) == bounties_before


def test_admin_can_review_bounty_request():
    r = client.get("/admin/bounty-requests", headers=_admin)
    assert r.status_code == 200, r.text
    rid = r.json()["requests"][0]["id"]
    r = client.post(f"/admin/bounty-requests/{rid}/review", headers=_admin,
                    json={"status": "approved", "note": "looks good"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    # Non-admin is gated.
    r = client.get("/admin/bounty-requests")
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# 5. Public status page
# ---------------------------------------------------------------------------

def test_status_page_renders_real_numbers():
    r = client.get("/status")
    assert r.status_code == 200, r.text
    assert "DAiL // STATUS" in r.text
    assert "fd96d21" in r.text  # real deploy log entry
    assert "Agent Passport" in r.text
    # Counts match the public Observatory (external only, real numbers).
    obs = client.get("/observatory/public/data").json()["stats"]
    assert str(obs["external_agents"]) in r.text
    assert str(obs["open_bounties"]) in r.text


def test_status_data_machine_readable():
    r = client.get("/status/data")
    assert r.status_code == 200, r.text
    s = r.json()
    for key in ("service", "version", "deploy_commit", "boot_time",
                "uptime_seconds", "external_agents", "open_bounties",
                "deploy_log"):
        assert key in s, key
    assert s["service"] == "dail-agent-world"
    assert s["uptime_seconds"] >= 0
    assert len(s["deploy_log"]) >= 5
