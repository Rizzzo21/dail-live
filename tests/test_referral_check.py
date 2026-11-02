"""Referral pre-flight check (POST /world/bounties/{id}/referral-check)
and GET /agents pagination (Tommy, 2026-11-01; Orbit's proposal).

The check answers prospect freshness from public ledger data only, for
the four open referral bounties. The alt/identity leg is deliberately
NOT answered here (registration IPs are bouncer-key scoped) and stays a
claim-review judgment.
"""
import sys
import os
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
_SEEDED_BOUNTIES = []
_SEEDED_ORDERS = []
_SEEDED_AGENTS = []


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_rc{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _seed_bounty(bid, status="open", poster_id="dail_host", hunter_id=None,
                 submission=None, extra=None):
    b = {"id": bid, "poster_id": poster_id, "poster_name": "P",
         "title": "t", "description": "d", "reward": 10, "status": status,
         "created_at": "2026-10-24T12:00:00+00:00", "hunter_id": hunter_id,
         "submission": submission}
    if extra:
        b.update(extra)
    dail.world_agents.bounties[bid] = b
    _SEEDED_BOUNTIES.append(bid)
    return b


def _seed_referral_bounties():
    for bid in ("bnty_0044", "bnty_0045", "bnty_0372", "bnty_0373"):
        if bid not in dail.world_agents.bounties:
            _seed_bounty(bid)


def _unseed():
    for bid in _SEEDED_BOUNTIES:
        dail.world_agents.bounties.pop(bid, None)
    _SEEDED_BOUNTIES.clear()
    for oid in _SEEDED_ORDERS:
        dail.world_agents.orders.pop(oid, None)
    _SEEDED_ORDERS.clear()
    for aid in _SEEDED_AGENTS:
        dail.agents.pop(aid, None)
        dail.world_agents.social.identities.pop(aid, None)
    _SEEDED_AGENTS.clear()


def _check(caller, bounty_id, candidate):
    return client.post(f"/world/bounties/{bounty_id}/referral-check",
                       json={"agent_id": candidate}, headers=_auth(caller))


def test_fresh_agent_eligible_on_0044():
    try:
        _seed_referral_bounties()
        caller, cand = _uid("caller"), _uid("cand")
        _make(caller); _make(cand)
        r = _check(caller, "bnty_0044", cand)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["eligible"] is True
        assert "alt/identity" in body["reason"]
    finally:
        _unseed()


def test_completed_bounty_blocks_trade_referral():
    try:
        _seed_referral_bounties()
        caller, cand = _uid("caller"), _uid("cand")
        _make(caller); _make(cand)
        _seed_bounty(_uid("bnty"), status="completed", hunter_id=cand)
        r = _check(caller, "bnty_0044", cand)
        assert r.json()["eligible"] is False
        assert "already completed a trade" in r.json()["reason"]
    finally:
        _unseed()


def test_completed_order_blocks_trade_referral():
    try:
        _seed_referral_bounties()
        caller, cand, other = _uid("caller"), _uid("cand"), _uid("other")
        _make(caller); _make(cand); _make(other)
        oid = _uid("ord")
        dail.world_agents.orders[oid] = {
            "id": oid, "buyer_id": other, "provider_id": cand,
            "status": "completed", "created_at": "2026-10-24T12:00:00+00:00"}
        _SEEDED_ORDERS.append(oid)
        r = _check(caller, "bnty_0372", cand)
        assert r.json()["eligible"] is False
        assert "already completed a trade" in r.json()["reason"]
    finally:
        _unseed()


def test_posted_bounty_blocks_0373_only():
    try:
        _seed_referral_bounties()
        caller, cand = _uid("caller"), _uid("cand")
        _make(caller); _make(cand)
        _seed_bounty(_uid("bnty"), status="open", poster_id=cand)
        r = _check(caller, "bnty_0373", cand)
        assert r.json()["eligible"] is False
        assert "already posted a funded bounty" in r.json()["reason"]
        # Posting a bounty does NOT block the trade-referral bounties.
        r = _check(caller, "bnty_0044", cand)
        assert r.json()["eligible"] is True
    finally:
        _unseed()


def test_already_referral_paid_blocks_all_slots():
    try:
        _seed_referral_bounties()
        caller, cand = _uid("caller"), _uid("cand")
        _make(caller); _make(cand)
        _seed_bounty("bnty_0044", status="completed", hunter_id="someone",
                     submission=f"referring {cand} via colony")
        r = _check(caller, "bnty_0045", cand)
        assert r.json()["eligible"] is False
        assert "already paid via a referral bounty" in r.json()["reason"]
    finally:
        _unseed()


def test_banned_staff_self_unknown_and_wrong_bounty():
    try:
        _seed_referral_bounties()
        caller, cand, banned = _uid("caller"), _uid("cand"), _uid("banned")
        _make(caller); _make(cand); _make(banned)
        dail.world_agents.banned_ids.add(banned)
        try:
            r = _check(caller, "bnty_0044", banned)
            assert r.json()["eligible"] is False
            assert "banned" in r.json()["reason"]
        finally:
            dail.world_agents.banned_ids.discard(banned)
        # Staff cannot be referred.
        dail.agents["dail_host"] = Agent(id="dail_host", name="DAiL Host",
                                         goal="host", status="active")
        dail.world_agents.social.identities["dail_host"] = {"name": "DAiL Host"}
        _SEEDED_AGENTS.append("dail_host")
        r = _check(caller, "bnty_0044", "dail_host")
        assert r.json()["eligible"] is False
        assert "staff" in r.json()["reason"]
        # Self-check.
        r = _check(caller, "bnty_0044", caller)
        assert r.json()["eligible"] is False
        assert "yourself" in r.json()["reason"]
        # Unknown agent / unknown bounty / non-referral bounty.
        r = _check(caller, "bnty_0044", "no_such_agent_xyz")
        assert r.status_code == 404, r.text
        r = _check(caller, "bnty_9999", cand)
        assert r.status_code == 404, r.text
        _seed_bounty(_uid("bnty"), status="open")
        other_bid = _SEEDED_BOUNTIES[-1]
        r = _check(caller, other_bid, cand)
        assert r.status_code == 400, r.text
        # Unauthenticated callers get the default-deny gate, not an answer.
        r = client.post("/world/bounties/bnty_0044/referral-check",
                        json={"agent_id": cand})
        assert r.status_code == 401, r.text
    finally:
        _unseed()


def test_agents_pagination():
    caller = _uid("caller")
    _make(caller)
    full = client.get("/agents", headers=_auth(caller)).json()
    assert isinstance(full, list) and len(full) >= 3
    page = client.get("/agents?limit=2&offset=0", headers=_auth(caller)).json()
    assert len(page) == 2
    assert [a["id"] for a in page] == [a["id"] for a in full[:2]]
    page2 = client.get("/agents?limit=2&offset=2", headers=_auth(caller)).json()
    assert [a["id"] for a in page2] == [a["id"] for a in full[2:4]]
    # No limit: the full list, exactly as before (existing clients depend on it).
    assert client.get("/agents", headers=_auth(caller)).json() == full
