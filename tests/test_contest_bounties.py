"""Contest bounties (2026-10-20): multi-entry bounties for closest-wins
formats (predict micros). One claim-with-submission = one entry; the
bounty stays open; the poster settles the pot via /contest/award and
ties split. Built after bnty_0249 fielded exactly one entry under the
single-claim lease."""
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

ENTRY = ("my prediction and the full reasoning behind it, written out "
         "so the entry clears the hundred character submission floor")


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_ct{_seq[0]:04d}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _contest(poster, reward=30):
    r = client.post("/world/bounties", json={
        "agent_id": poster, "title": "Predict the count",
        "description": "Closest prediction wins, ties split.",
        "reward": reward, "contest": True}, headers=_auth(poster))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enter(hunter, bid, text=ENTRY):
    return client.post(f"/world/bounties/{bid}/claim", json={
        "agent_id": hunter, "submission": text}, headers=_auth(hunter))


def test_contest_collects_multiple_entries_and_stays_open():
    poster = _uid("p")
    hunters = [_uid("h") for _ in range(3)]
    _make(poster)
    for h in hunters:
        _make(h)
    bid = _contest(poster)
    for h in hunters:
        r = _enter(h, bid)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "open"
    b = dail.world_agents.bounties[bid]
    assert b["contest"] is True
    assert [e["agent_id"] for e in b["entries"]] == hunters
    # One entry per agent.
    r = _enter(hunters[0], bid)
    assert r.status_code == 400
    # Entries require a submission (no bare reservations).
    h4 = _uid("h")
    _make(h4)
    r = client.post(f"/world/bounties/{bid}/claim",
                    json={"agent_id": h4}, headers=_auth(h4))
    assert r.status_code == 422
    # The poster cannot enter their own contest.
    r = _enter(poster, bid)
    assert r.status_code == 400


def test_contest_award_single_winner_fee_waived_for_new_poster():
    poster, h1, h2 = _uid("p"), _uid("h"), _uid("h")
    for x in (poster, h1, h2):
        _make(x)
    bid = _contest(poster, reward=30)
    _enter(h1, bid)
    _enter(h2, bid)
    before = _bal(h1)
    r = client.post(f"/world/bounties/{bid}/contest/award", json={
        "agent_id": poster, "winner_ids": [h1]}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "completed"
    assert b["contest_winners"] == [h1]
    # Fresh non-house poster: fee waived on the first 3, winner gets all 30.
    assert b["fee"] == 0 and b["fee_waived"] is True
    assert _bal(h1) == before + 30
    assert _bal(h2) == 100


def test_contest_award_tie_splits_pot_and_dust_refunds():
    poster, h1, h2, h3 = _uid("p"), _uid("h"), _uid("h"), _uid("h")
    for x in (poster, h1, h2, h3):
        _make(x)
    # Burn the poster's 3 fee waivers with normal bounties so the contest
    # settles under the standard 10% fee.
    for i in range(3):
        bid0 = client.post("/world/bounties", json={
            "agent_id": poster, "title": f"w{i}", "description": "d",
            "reward": 3}, headers=_auth(poster)).json()["id"]
        client.post(f"/world/bounties/{bid0}/claim", json={
            "agent_id": h1, "submission": ENTRY}, headers=_auth(h1))
        r = client.post(f"/world/bounties/{bid0}/accept", json={
            "agent_id": poster}, headers=_auth(poster))
        assert r.status_code == 200, r.text
    bid = _contest(poster, reward=25)
    for h in (h1, h2, h3):
        _enter(h, bid)
    p_before, w_before = _bal(poster), _bal(h1)
    r = client.post(f"/world/bounties/{bid}/contest/award", json={
        "agent_id": poster, "winner_ids": [h1, h2]}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    b = r.json()
    # fee = 2 (10% of 25), net 23, split 11/11, dust 1 back to the poster.
    assert b["fee"] == 2 and b["contest_payout_each"] == 11
    assert _bal(h1) == w_before + 11
    assert _bal(h2) == 100 + 11
    assert _bal(poster) == p_before + 1
    assert _bal(h3) == 100


def test_contest_award_guards():
    poster, h1, stranger = _uid("p"), _uid("h"), _uid("s")
    for x in (poster, h1, stranger):
        _make(x)
    bid = _contest(poster)
    _enter(h1, bid)
    # No winners named.
    r = client.post(f"/world/bounties/{bid}/contest/award", json={
        "agent_id": poster, "winner_ids": []}, headers=_auth(poster))
    assert r.status_code == 400
    # Winner must be an entrant.
    r = client.post(f"/world/bounties/{bid}/contest/award", json={
        "agent_id": poster, "winner_ids": [stranger]}, headers=_auth(poster))
    assert r.status_code == 400
    # Only the poster settles.
    r = client.post(f"/world/bounties/{bid}/contest/award", json={
        "agent_id": h1, "winner_ids": [h1]}, headers=_auth(h1))
    assert r.status_code == 403
    # A normal bounty cannot be contest-awarded.
    bid2 = client.post("/world/bounties", json={
        "agent_id": poster, "title": "plain", "description": "d",
        "reward": 5}, headers=_auth(poster)).json()["id"]
    r = client.post(f"/world/bounties/{bid2}/contest/award", json={
        "agent_id": poster, "winner_ids": [h1]}, headers=_auth(poster))
    assert r.status_code == 400
    # And a contest cannot be single-accepted.
    r = client.post(f"/world/bounties/{bid}/accept", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 400


def test_contest_cancel_refunds_escrow():
    poster, h1 = _uid("p"), _uid("h")
    for x in (poster, h1):
        _make(x)
    before = _bal(poster)
    bid = _contest(poster, reward=10)
    _enter(h1, bid)
    r = client.post(f"/world/bounties/{bid}/cancel", json={
        "agent_id": poster}, headers=_auth(poster))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert _bal(poster) == before
