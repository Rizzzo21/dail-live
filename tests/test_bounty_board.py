"""HTML bounty board (/bounties): public, server-rendered, open bounties
only, whitelist projection. The raw bounty record carries internal fields
(failed_claims, lapsed_hunters, rejected_hunters, submission) that must
never appear on the board, and the JSON endpoint must keep returning
JSON exactly as before.
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
from dail.api import app, _bb_board_html, _bb_eligibility, _bb_deadline

client = TestClient(app)

_seq = [0]
_SEEDED = []


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_bb{_seq[0]}"


def _seed_open(title, description, reward=4, listing_expires_at=None,
                work_window_hours=24, extra=None):
    from dail.api import dail
    bid = _uid("bnty")
    b = {"id": bid, "poster_id": "bb_poster", "poster_name": "Board Poster",
         "title": title, "description": description, "reward": reward,
         "status": "open", "created_at": "2026-10-24T12:00:00+00:00",
         "listing_expires_at": listing_expires_at,
         "work_window_hours": work_window_hours}
    if extra:
        b.update(extra)
    dail.world_agents.bounties[bid] = b
    _SEEDED.append(bid)
    return b


def _unseed():
    from dail.api import dail
    for bid in _SEEDED:
        dail.world_agents.bounties.pop(bid, None)
    _SEEDED.clear()


def test_board_is_public_and_renders_open_bounty():
    b = _seed_open("Board Test Job", "Do the thing, get paid.")
    try:
        r = client.get("/bounties")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Board Test Job" in r.text
        assert "4 DAIL" in r.text
        assert "Board Poster" in r.text
        assert b["id"] in r.text
    finally:
        _unseed()


def test_board_shows_eligibility_and_deadline():
    desc = ("HOUSE-SEEDED STARTER JOB.\n\n"
            "ELIGIBILITY: You may claim this job only if you are an "
            "external agent with zero completed bounties at claim time.\n\n"
            "WHAT TO DO: Check the thing.")
    _seed_open("STARTER JOB: Board Eligibility",
               desc, reward=5,
               listing_expires_at="2026-10-31T21:45:00+00:00",
               work_window_hours=24)
    try:
        r = client.get("/bounties")
        assert "external agent with zero completed bounties" in r.text
        assert "2026-10-31 21:45 UTC" in r.text
        assert "24h to submit after claiming" in r.text
    finally:
        _unseed()


def test_board_grandfathered_deadline_and_no_eligibility():
    _seed_open("Board Plain Job", "No eligibility paragraph here.")
    try:
        r = client.get("/bounties")
        assert "Board Plain Job" in r.text
        assert "No listing expiry (grandfathered)" in r.text
        assert "No eligibility paragraph here." in r.text
    finally:
        _unseed()


def test_board_hides_internal_fields():
    _seed_open("Board Leak Job", "Harmless description.",
               extra={"failed_claims": 2,
                      "lapsed_hunters": ["bb_leak_hunter"],
                      "rejected_hunters": ["bb_leak_hunter"],
                      "submission": "bb_leak_submission_text"})
    try:
        r = client.get("/bounties")
        assert "Board Leak Job" in r.text
        for leaked in ("failed_claims", "lapsed_hunters",
                       "rejected_hunters", "bb_leak_hunter",
                       "bb_leak_submission_text"):
            assert leaked not in r.text
    finally:
        _unseed()


def test_board_empty_state_renders():
    html = _bb_board_html([])
    assert "No open bounties right now." in html


def test_eligibility_and_deadline_helpers():
    assert _bb_eligibility("ELIGIBILITY: Only newcomers.\n\nWHAT TO DO: x") \
        == "Only newcomers."
    assert _bb_eligibility("No such paragraph.") == ""
    assert _bb_deadline({"listing_expires_at": None}) \
        == "No listing expiry (grandfathered)"
    assert _bb_deadline({"listing_expires_at": "2026-10-31T21:45:00+00:00"}) \
        == "2026-10-31 21:45 UTC"


def test_json_endpoint_unchanged():
    b = _seed_open("Board JSON Job", "Still JSON on the API.")
    try:
        r = client.get("/world/bounties?status=open")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        data = r.json()
        assert "bounties" in data
        assert any(x["id"] == b["id"] for x in data["bounties"])
    finally:
        _unseed()
