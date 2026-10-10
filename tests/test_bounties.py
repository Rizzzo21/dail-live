import sys
import os
from datetime import datetime, timezone, timedelta
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
    return f"{prefix}_bn{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _treasury():
    return client.get("/treasury").json()["balance"]


def test_bounty_full_lifecycle():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    t0 = _treasury()
    # post: reward escrowed immediately
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Need a logo",
        "description": "SVG logo for an agent marketplace", "reward": 50},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["id"].startswith("bnty_") and b["status"] == "open"
    assert _bal(poster) == 50  # 100 - 50 held
    # public listing shows it
    r = client.get("/world/bounties")
    assert any(x["id"] == b["id"] for x in r.json()["bounties"])
    # claim
    r = client.post(f"/world/bounties/{b['id']}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "claimed"
    # accept: hunter nets 45, treasury takes 5 (10%)
    r = client.post(f"/world/bounties/{b['id']}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert _bal(hunter) == 145
    assert _treasury() == t0 + 5


def test_bounty_cancel_refunds():
    poster = _uid("p")
    _make(poster)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/cancel",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _bal(poster) == 100


def test_bounty_guards():
    poster, hunter, stranger = _uid("p"), _uid("h"), _uid("s")
    _make(poster); _make(hunter); _make(stranger)
    # reward too small
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 1},
        headers=_auth(poster))
    assert r.status_code == 400
    # unauthenticated
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10})
    assert r.status_code == 401
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 10},
        headers=_auth(poster)).json()["id"]
    # cannot claim own bounty
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": poster, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(poster))
    assert r.status_code == 400
    # stranger cannot accept
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": stranger}, headers=_auth(stranger))
    assert r.status_code == 403
    # cannot claim twice (409: lost the race / already taken)
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": stranger, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(stranger))
    assert r.status_code == 409


def test_ban_hunter_voids_claim_reopens_bounty():
    # Banning the hunter of a claimed bounty: the claim dies, the bounty
    # reopens, escrow stays held for the next hunter (poster is innocent).
    from dail.api import dail as _d
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Need work",
        "description": "do the thing", "reward": 40},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    _d.ban_agent(hunter, "test")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "open", b
    assert b.get("hunter_id") is None
    assert hunter in _d.world_agents.banned_ids
    # poster balance unchanged (escrow still held: 100 - 40)
    r = client.get(f"/ledger/{poster}", headers=_auth(poster))
    assert r.json()["balance"] == 60
    # unban clears the sweep guard
    _d.unban_agent(hunter)
    assert hunter not in _d.world_agents.banned_ids


def test_referral_reward_fires_on_bounty_track():
    # Referrers earn when their invitee's first real earning is a bounty,
    # not just a trade/order. Dust gate still applies (reward 25 >= 10).
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    referrer, hunter, poster = _uid("r"), _uid("h"), _uid("p")
    # distinct public registration IPs: the TestClient reports client.host as
    # "testclient" for all requests, which would trip the shared-IP wash guard
    def _mk(aid, ip, **kw):
        r = client.post("/agents", json={"id": aid, "name": aid, **kw},
                        headers={"X-Forwarded-For": ip})
        assert r.status_code == 200, r.text
        _keys[aid] = r.json()["api_key"]
    _mk(referrer, "9.80.0.1"); _mk(poster, "9.80.0.2")
    _mk(hunter, "9.80.0.3", referred_by=referrer)
    _d.world_agents.agent_created[poster] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _d.world_agents.agent_created[hunter] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Audit this",
        "description": "real work", "reward": 25},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    bal_before = client.get(f"/ledger/{referrer}",
                            headers=_auth(referrer)).json()["balance"]
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    bal_after = client.get(f"/ledger/{referrer}",
                           headers=_auth(referrer)).json()["balance"]
    assert bal_after == bal_before + 10, (bal_before, bal_after)


def test_bounty_post_notifies_matching_capabilities():
    # Re-engagement: posting a bounty notifies agents whose profile
    # capabilities match the bounty text. Poster, banned, and capability-less
    # agents are never notified.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, hunter, other = _uid("p"), _uid("h"), _uid("o")
    _make(poster); _make(hunter); _make(other)
    wa.update_profile(hunter, "I audit smart contracts",
                      ["security", "auditing", "solidity"])
    wa.update_profile(other, "I write poetry", ["poetry", "writing"])
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Security audit needed",
        "description": "Audit this Solidity contract for vulnerabilities",
        "reward": 30},
        headers=_auth(poster))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    def _notifs(aid):
        return client.get(f"/world/notifications/{aid}",
                          headers=_auth(aid)).json()["notifications"]
    hn = [n for n in _notifs(hunter) if n.get("bounty_id") == bid]
    assert hn and hn[0]["type"] == "bounty_match", hn
    on = [n for n in _notifs(other) if n.get("bounty_id") == bid]
    assert not on
    pn = [n for n in _notifs(poster) if n.get("bounty_id") == bid]
    assert not pn


def test_bounty_v2_listing_and_work_windows():
    # Lifecycle v2: listings live 7 days fixed; work window defaults to 24h,
    # settable 1..168 at posting. claim_window_hours still accepted
    # (deprecated) but drives nothing.
    from datetime import datetime, timezone
    p = _uid("p"); _make(p)
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Quick job", "description": "fast",
        "reward": 10}, headers=_auth(p))
    assert r.status_code == 201, r.text
    b = r.json()
    lexp = datetime.fromisoformat(b["listing_expires_at"])
    created = datetime.fromisoformat(b["created_at"])
    assert abs((lexp - created).total_seconds() - 7 * 86400) < 60
    assert b["work_window_hours"] == 24
    assert b["expires_at"] is None  # retired field
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Slow job", "description": "slow",
        "reward": 10, "work_window_hours": 168}, headers=_auth(p))
    assert r.status_code == 201, r.text
    assert r.json()["work_window_hours"] == 168
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Too slow", "description": "x",
        "reward": 10, "work_window_hours": 169}, headers=_auth(p))
    assert r.status_code == 422, r.text  # schema caps at 168
    # deprecated param still accepted, no behavior
    r = client.post("/world/bounties", json={
        "agent_id": p, "title": "Legacy", "description": "x",
        "reward": 10, "claim_window_hours": 72}, headers=_auth(p))
    assert r.status_code == 201, r.text


def test_reserve_claim_then_submit():
    # Claim is a reserve: no submission required. Submit delivers the work
    # and starts the 72h review clock. Second submit is rejected.
    from datetime import datetime, timezone
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "claimed" and b["submitted_at"] is None
    assert b["stall_deadline"] is not None and b["work_deadline"] is not None
    r = client.post(f"/world/bounties/{bid}/submit", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["submitted_at"] is not None and b["review_deadline"] is not None
    assert b["stall_deadline"] is None
    r = client.post(f"/world/bounties/{bid}/submit", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 400, r.text


def test_atomic_claim_with_submission():
    # Non-blank submission on claim = atomic claim+submit (old one-shot flow).
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["submitted_at"] == b["claimed_at"]
    assert b["review_deadline"] is not None
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_stall_lapse_reopens_snipable_bars_hunter():
    # Reserve with no submission past the 4h stall limit: claim lapses,
    # bounty reopens, the stalled hunter is barred from reclaiming, a
    # sniper can claim it.
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster, hunter, sniper = _uid("p"), _uid("h"), _uid("s")
    _make(poster); _make(hunter); _make(sniper)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = _d.world_agents.bounties[bid]
    b["stall_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    client.get("/world/bounties")  # board read triggers sweep
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "open", b
    assert b.get("hunter_id") is None and b["failed_claims"] == 1
    assert hunter in b["lapsed_hunters"]
    # stalled hunter is barred from reclaiming this bounty
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 400, r.text
    # sniper gets a fresh reserve with a fresh 24h work clock
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": sniper}, headers=_auth(sniper))
    assert r.status_code == 200, r.text
    assert r.json()["work_deadline"] is not None


def test_two_lapses_pause_then_relist():
    # Second lapsed claim pauses the bounty; poster relists or cancels.
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster, h1, h2 = _uid("p"), _uid("h1"), _uid("h2")
    _make(poster); _make(h1); _make(h2)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    for h in (h1, h2):
        r = client.post(f"/world/bounties/{bid}/claim", json={
            "agent_id": h}, headers=_auth(h))
        assert r.status_code == 200, r.text
        b = _d.world_agents.bounties[bid]
        b["stall_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "paused", b
    assert b["failed_claims"] == 2
    # paused bounties cannot be claimed
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": h1}, headers=_auth(h1))
    assert r.status_code == 409, r.text
    # poster relists: open again, counters reset, listing clock kept
    lexp = b["listing_expires_at"]
    r = client.post(f"/world/bounties/{bid}/relist", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "open" and b["failed_claims"] == 0
    assert b["listing_expires_at"] == lexp
    # and a paused bounty can also just be cancelled (escrow refunded)
    bid2 = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t2", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    h3, h4 = _uid("h3"), _uid("h4")
    _make(h3); _make(h4)
    for h in (h3, h4):
        r = client.post(f"/world/bounties/{bid2}/claim", json={
            "agent_id": h}, headers=_auth(h))
        assert r.status_code == 200, r.text
        b = _d.world_agents.bounties[bid2]
        b["stall_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        client.get("/world/bounties")
    assert _d.world_agents.bounties[bid2]["status"] == "paused"
    r = client.post(f"/world/bounties/{bid2}/cancel", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _bal(poster) == 80  # bid2's 20 refunded; bid's 20 still escrowed


def test_work_deadline_lapse():
    # Hunter reserves but never submits past the work window: lapse, even
    # though the 4h stall limit hasn't been touched in this scenario.
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20,
        "work_window_hours": 1}, headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = _d.world_agents.bounties[bid]
    # work deadline (1h) is past; push the 4h stall deadline out so only the
    # work-deadline path can fire
    b["work_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    b["stall_deadline"] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "open", b
    assert hunter in b["lapsed_hunters"]


def test_extension_suspends_timers_and_approve_extends():
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    b = _d.world_agents.bounties[bid]
    wd0 = b["work_deadline"]
    # blow the stall deadline, then request an extension: pending request
    # suspends the lapse timers
    b["stall_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    r = client.post(f"/world/bounties/{bid}/request-extension", json={
        "agent_id": hunter, "reason": "need more time for research",
        "extra_hours": 10}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert r.json()["extension_request"]["status"] == "pending"
    client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "claimed", b  # timers suspended: no lapse
    # poster approves: work_deadline pushed out by 10h
    r = client.post(f"/world/bounties/{bid}/extension/approve", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["extension_request"]["status"] == "approved"
    wd1 = datetime.fromisoformat(b["work_deadline"])
    assert abs((wd1 - datetime.fromisoformat(wd0)).total_seconds() - 10 * 3600) < 60
    # second request denied: timers resume, blown stall deadline lapses it
    r = client.post(f"/world/bounties/{bid}/request-extension", json={
        "agent_id": hunter, "reason": "still stuck", "extra_hours": 5},
        headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{bid}/extension/deny", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "open", b
    assert hunter in b["lapsed_hunters"]


def test_extension_cumulative_cap_72h():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    client.post(f"/world/bounties/{bid}/request-extension", json={
        "agent_id": hunter, "reason": "big job", "extra_hours": 72},
        headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/extension/approve", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    # cumulative extra would exceed 72h: rejected
    client.post(f"/world/bounties/{bid}/request-extension", json={
        "agent_id": hunter, "reason": "more", "extra_hours": 1},
        headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/extension/approve", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 400, r.text
    # non-hunter cannot request; non-poster cannot approve
    stranger = _uid("s"); _make(stranger)
    r = client.post(f"/world/bounties/{bid}/request-extension", json={
        "agent_id": stranger, "reason": "x", "extra_hours": 1},
        headers=_auth(stranger))
    assert r.status_code == 403, r.text


def test_withdraw_after_72h_no_review():
    # Submitted work the poster ignores for 72h: hunter withdraws, bounty
    # reopens, hunter may reclaim (not barred).
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    # too early: poster still has review time
    r = client.post(f"/world/bounties/{bid}/withdraw", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 400, r.text
    b = _d.world_agents.bounties[bid]
    b["review_deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    r = client.post(f"/world/bounties/{bid}/withdraw", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "open" and b["submission"] is None
    assert hunter not in b.get("lapsed_hunters", [])
    # hunter may reclaim
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text


def test_raise_topup_escrow_clock_untouched():
    poster, stranger = _uid("p"), _uid("s")
    _make(poster); _make(stranger)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    lexp0 = _d_world()[bid]["listing_expires_at"]
    assert _bal(poster) == 80
    r = client.post(f"/world/bounties/{bid}/raise", json={
        "agent_id": poster, "amount": 15}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["reward"] == 35
    assert b["listing_expires_at"] == lexp0  # clock untouched
    assert _bal(poster) == 65  # 100 - 20 - 15
    # second raise works (unique idempotency key per raise)
    r = client.post(f"/world/bounties/{bid}/raise", json={
        "agent_id": poster, "amount": 5}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["reward"] == 40
    # stranger cannot raise
    r = client.post(f"/world/bounties/{bid}/raise", json={
        "agent_id": stranger, "amount": 5}, headers=_auth(stranger))
    assert r.status_code == 403, r.text
    # cannot raise a claimed bounty
    hunter = _uid("h"); _make(hunter)
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/raise", json={
        "agent_id": poster, "amount": 5}, headers=_auth(poster))
    assert r.status_code == 400, r.text


def _d_world():
    from dail.api import dail as _d
    return _d.world_agents.bounties


def test_listing_warning_and_expiry():
    # Poster warned <24h before listing expiry; past expiry the bounty
    # expires and escrow returns to the poster.
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    poster = _uid("p"); _make(poster)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    b = _d.world_agents.bounties[bid]
    b["listing_expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["warned_listing"] is True
    notifs = client.get(f"/world/notifications/{poster}",
                        headers=_auth(poster)).json()["notifications"]
    assert any(n.get("type") == "bounty_expiring" and n.get("bounty_id") == bid
               for n in notifs), notifs
    # warning fires only once
    n0 = len(notifs)
    client.get("/world/bounties")
    notifs = client.get(f"/world/notifications/{poster}",
                        headers=_auth(poster)).json()["notifications"]
    assert len([n for n in notifs if n.get("type") == "bounty_expiring"
                and n.get("bounty_id") == bid]) == 1
    assert len(notifs) == n0
    # past expiry: sweep expires it, escrow refunded
    b["listing_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    client.get("/world/bounties")
    b = _d.world_agents.bounties[bid]
    assert b["status"] == "expired", b
    assert _bal(poster) == 100
    # expired bounty cannot be claimed (sweep runs inside claim)
    hunter = _uid("h"); _make(hunter)
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 409, r.text


def test_lifecycle_v2_migration_grandfathers():
    # Pre-v2 records (no listing_expires_at key) migrate idempotently:
    # never expire, retired fields nulled, claimed ones get review clocks
    # backfilled so nothing instantly expires.
    from datetime import datetime, timedelta, timezone
    from dail.api import dail as _d
    wa = _d.world_agents
    old_open = {"id": "bnty_old1", "poster_id": "x", "poster_name": "x",
                "title": "t", "description": "d", "reward": 10,
                "private_submission": False, "claim_window_hours": 4,
                "expires_at": "2020-01-01T00:00:00+00:00",
                "status": "open", "hunter_id": None, "submission": None,
                "rejected_hunters": [], "created_at": "2020-01-01T00:00:00+00:00",
                "claimed_at": None, "completed_at": None}
    old_claimed = dict(old_open, id="bnty_old2", status="claimed",
                       hunter_id="h", submission="Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes.",
                       claimed_at="2026-10-01T00:00:00+00:00")
    wa.bounties["bnty_old1"] = old_open
    wa.bounties["bnty_old2"] = old_claimed
    wa._migrate_bounty_lifecycle_v2()
    b1, b2 = wa.bounties["bnty_old1"], wa.bounties["bnty_old2"]
    assert b1["listing_expires_at"] is None and b1["expires_at"] is None
    assert b1["warned_listing"] is True and b1["failed_claims"] == 0
    assert b1["lapsed_hunters"] == [] and b1["extension_request"] is None
    # claimed record: review clock backfilled into the future
    assert b2["submitted_at"] is not None
    assert datetime.fromisoformat(b2["review_deadline"]) > datetime.now(timezone.utc)
    assert datetime.fromisoformat(b2["work_deadline"]) > datetime.now(timezone.utc)
    # idempotent: second run changes nothing
    snap = {k: dict(v) for k, v in wa.bounties.items() if k.startswith("bnty_old")}
    wa._migrate_bounty_lifecycle_v2()
    for k in snap:
        assert wa.bounties[k] == snap[k], k
    # grandfathered open bounty never expires, even with a sweep
    wa.bounties["bnty_old1"]["listing_expires_at"] = None
    res = wa.sweep_bounties()
    assert wa.bounties["bnty_old1"]["status"] == "open"
    del wa.bounties["bnty_old1"]; del wa.bounties["bnty_old2"]


# ---------------------------------------------------------------------------
# Regression tests: bug-hunt audit on lifecycle v2 (2026-10-08)
# ---------------------------------------------------------------------------

def test_accept_requires_submission():
    # A bare reserve (no work delivered) must NOT release escrow on accept.
    # Pre-v2 this was structurally impossible (claim required a submission).
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    hb0 = _bal(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 400, r.text
    assert _bal(hunter) == hb0  # no payout without work
    # After the hunter submits, accept works as before.
    r = client.post(f"/world/bounties/{bid}/submit", json={
        "agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."}, headers=_auth(hunter))
    assert r.status_code == 200
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert _bal(hunter) == hb0 + 18  # 20 minus the 10% fee


def test_bounty_seq_unique_under_concurrency():
    # Concurrent posts must never mint duplicate bnty_NNNN ids (record
    # overwrite + idem-deduped escrow = underfunded bounty).
    import threading
    poster = _uid("p")
    _make(poster)
    ids, errs = [], []
    def do_post(i):
        try:
            r = client.post("/world/bounties", json={
                "agent_id": poster, "title": f"t{i}", "description": "d",
                "reward": 2}, headers=_auth(poster))
            assert r.status_code == 201, r.text
            ids.append(r.json()["id"])
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
    ts = [threading.Thread(target=do_post, args=(i,)) for i in range(20)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errs, errs
    assert len(ids) == 20 and len(set(ids)) == 20


def test_extension_cap_resets_on_new_claim():
    # Hunter A burns the 72h cumulative extension cap, then lapses. Hunter B's
    # fresh claim must get a full cap, not A's leftovers.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, ha, hb = _uid("p"), _uid("ha"), _uid("hb")
    _make(poster); _make(ha); _make(hb)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={"agent_id": ha},
                headers=_auth(ha))
    client.post(f"/world/bounties/{bid}/request-extension",
                json={"agent_id": ha, "reason": "need time", "extra_hours": 72},
                headers=_auth(ha))
    r = client.post(f"/world/bounties/{bid}/extension/approve",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200
    # Force A's claim to lapse.
    wa.bounties[bid]["stall_deadline"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wa.sweep_bounties()
    assert wa.bounties[bid]["status"] == "open"
    # Hunter B claims and gets a full extension cap.
    r = client.post(f"/world/bounties/{bid}/claim", json={"agent_id": hb},
                    headers=_auth(hb))
    assert r.status_code == 200
    assert wa.bounties[bid]["extension_hours_used"] == 0
    client.post(f"/world/bounties/{bid}/request-extension",
                json={"agent_id": hb, "reason": "need time", "extra_hours": 10},
                headers=_auth(hb))
    r = client.post(f"/world/bounties/{bid}/extension/approve",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text


def test_auto_accept_measured_from_submission():
    # The 7-day review SLA runs from submission, not claim: a hunter who
    # submits late must not shorten the poster's review window.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={"agent_id": hunter},
                headers=_auth(hunter))
    b = wa.bounties[bid]
    b["claimed_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    b["submitted_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    b["submission"] = "late work"
    b["review_deadline"] = (datetime.now(timezone.utc) + timedelta(hours=71)).isoformat()
    wa.sweep_bounties()
    # Submitted 1h ago: must still be awaiting review, not auto-accepted.
    assert wa.bounties[bid]["status"] == "claimed"
    # And it DOES auto-accept 7 days after submission.
    b["submitted_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    wa.sweep_bounties()
    assert wa.bounties[bid]["status"] == "completed"


def test_release_claim_clears_settle_failed():
    # A stale settlement-failure flag must not linger on a reopened bounty and
    # pollute the admin reconciliation endpoint.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim",
                json={"agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."},
                headers=_auth(hunter))
    wa.bounties[bid]["settle_failed"] = {"error": "x", "at": "y"}
    r = client.post(f"/world/bounties/{bid}/release",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200
    assert wa.bounties[bid].get("settle_failed") is None


def test_submission_minimum_length_enforced():
    # Anti-farming floor (2026-10-08): 1-char submissions + colluding
    # poster accepts were a wash-trade vector. Submissions <100 chars
    # are rejected at both claim and submit.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    # claim with a short submission -> 422
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": hunter, "submission": "x" * 50},
                    headers=_auth(hunter))
    assert r.status_code == 422, r.text
    # bare reserve still works
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": hunter}, headers=_auth(hunter))
    assert r.status_code == 200, r.text
    # submit with a short submission -> 422
    r = client.post(f"/world/bounties/{bid}/submit",
                    json={"agent_id": hunter, "submission": "short"},
                    headers=_auth(hunter))
    assert r.status_code == 422, r.text
    # exactly 100 chars is accepted
    ok = "y" * 100
    r = client.post(f"/world/bounties/{bid}/submit",
                    json={"agent_id": hunter, "submission": ok},
                    headers=_auth(hunter))
    assert r.status_code == 200, r.text
    assert wa.bounties[bid]["submission"] == ok


def test_accept_audit_logs_poster_and_hunter_ips():
    # Wash-trade signal: the bounty.completed audit event carries both
    # registration IPs so the bouncer can flag same-IP poster/hunter pairs.
    from dail.api import dail as _d
    wa = _d.world_agents
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    wa.agent_ips[poster] = "203.0.113.7"
    wa.agent_ips[hunter] = "203.0.113.7"
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    before = len(_d.audit.events)
    client.post(f"/world/bounties/{bid}/claim",
                json={"agent_id": hunter,
                      "submission": "z" * 150}, headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/accept",
                    json={"agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    completed = [e for e in _d.audit.events[before:]
                 if e["event"] == "bounty.completed" and e["payload"]["bounty_id"] == bid]
    assert completed, "bounty.completed audit event missing"
    payload = completed[-1]["payload"]
    assert payload["poster_ip"] == "203.0.113.7"
    assert payload["hunter_ip"] == "203.0.113.7"


def test_bounty_reject_carries_reason():
    # Poster can attach a reason; the hunter sees it in notifications.
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter,
        "submission": "Completed the requested deliverable and verified it against the bounty requirements; full summary of changes and test evidence in the attached notes."},
        headers=_auth(hunter))
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{bid}/reject", json={
        "agent_id": poster, "reason": "Fabricated numbers. Do better!"},
        headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    r = client.get(f"/world/notifications/{hunter}", headers=_auth(hunter))
    bodies = [n.get("body", "") for n in r.json()["notifications"]]
    assert any("Fabricated numbers" in b and "Do better" in b for b in bodies), bodies
    # no reason: legacy body unchanged (fresh bounty, same hunter)
    bid2 = client.post("/world/bounties", json={
        "agent_id": poster, "title": "t2", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid2}/claim", json={
        "agent_id": hunter,
        "submission": "Completed the requested deliverable and verified it against the bounty requirements; full summary of changes and test evidence in the attached notes."},
        headers=_auth(hunter))
    assert r.status_code == 200, r.text
    client.post(f"/world/bounties/{bid2}/reject", json={"agent_id": poster},
                headers=_auth(poster))
    r = client.get(f"/world/notifications/{hunter}", headers=_auth(hunter))
    bodies = [n.get("body", "") for n in r.json()["notifications"]]
    assert any(b == f"Bounty {bid2}: the poster declined your submission." for b in bodies)


def test_public_observatory_paid_wall():
    # Completed external bounties appear on the wall with post-fee payout.
    poster, hunter = _uid("p"), _uid("h")
    _make(poster); _make(hunter)
    bid = client.post("/world/bounties", json={
        "agent_id": poster, "title": "wall job", "description": "d", "reward": 20},
        headers=_auth(poster)).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter,
        "submission": "Completed the requested deliverable and verified it against the bounty requirements; full summary of changes and test evidence in the attached notes."},
        headers=_auth(hunter))
    r = client.post(f"/world/bounties/{bid}/accept", json={"agent_id": poster},
                    headers=_auth(poster))
    assert r.status_code == 200, r.text
    wall = client.get("/observatory/public/data").json().get("paid_wall", [])
    hits = [w for w in wall if w["bounty_id"] == bid]
    assert hits, "completed bounty should appear on the Paid Work Wall"
    w = hits[0]
    assert w["paid"] == 18.0  # 20 DAIL minus the 10% fee
    assert w["hunter_id"] == hunter and w["completed_at"]
