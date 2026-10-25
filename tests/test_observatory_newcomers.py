"""Daily newcomers leaderboard (Spec 3): first settled escrow within the
trailing 30 days (UTC), ranked by today's settled escrows.

Settled = completed bounties + completed/resolved orders with a non-null
completed_at. External agents only; staff and banned excluded. Empty day
returns rows [], never invented rows. Submission text never appears.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail, _po_newcomers
from dail.models import Agent

client = TestClient(app)

# Fixed "today" for deterministic UTC-day / window tests. Real now is
# 2026-10-25 UTC, so these seeded records never collide with other tests'
# real-today activity, and the empty-day test (2030) is immune to it.
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
_seq = [0]


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_nc{_seq[0]}"


def _register(aid):
    if aid in dail.agents:
        return dail.agents[aid]
    agent, _key = dail.create_agent(
        Agent(id=aid, name=aid, goal="newcomers test", balance=100))
    return agent


def _seed_bounty(bid, hunter, reward, completed_at, title="NC job",
                  fee=None, fee_waived=False, submission=None,
                  status="completed"):
    b = {"id": bid, "poster_id": "nc_poster", "poster_name": "nc_poster",
         "title": title, "description": "newcomers test bounty",
         "reward": reward, "status": status, "hunter_id": hunter,
         "created_at": completed_at, "completed_at": completed_at,
         "fee": fee, "fee_waived": fee_waived}
    if submission is not None:
        b["submission"] = submission
    dail.world_agents.bounties[bid] = b
    return b


def _seed_order(oid, provider, amount, fee, completed_at,
                status="completed", service_name="NC service"):
    o = {"id": oid, "service_id": "nc_svc", "service_name": service_name,
         "provider_id": provider, "buyer_id": "nc_buyer",
         "amount": amount, "fee": fee, "status": status,
         "created_at": completed_at, "completed_at": completed_at}
    dail.world_agents.orders[oid] = o
    return o


def _board(now=NOW):
    return dail.newcomers_board(now=now)


def _row_for(board, aid):
    for r in board["rows"]:
        if r["id"] == aid:
            return r
    return None


def test_newcomers_key_in_public_data_and_standalone_route():
    r = client.get("/observatory/public/data")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "newcomers" in body
    nc = body["newcomers"]
    assert nc["date_utc"] and nc["window"]
    assert isinstance(nc["rows"], list)
    s = client.get("/observatory/public/newcomers")
    assert s.status_code == 200, s.text
    standalone = s.json()
    assert standalone["date_utc"] == nc["date_utc"]
    assert {row["id"] for row in standalone["rows"]} == \
        {row["id"] for row in nc["rows"]}


def test_public_page_renders_newcomers_section():
    r = client.get("/observatory/public")
    assert r.status_code == 200, r.text
    assert "NEWCOMERS — TODAY (UTC)" in r.text
    assert "First settled escrow within the last 30 days" in r.text


def test_ssr_empty_state_exact_text():
    html = _po_newcomers({"newcomers": {"date_utc": "2026-10-24",
                                        "rows": []}})
    assert "NEWCOMERS — TODAY (UTC)" in html
    assert "No newcomer has settled an escrow today yet." in html


def test_empty_day_returns_empty_rows():
    board = _board(now=datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert board["date_utc"] == "2030-01-01"
    assert board["rows"] == []


def test_eligibility_window_first_settlement_too_old():
    old = _uid("old")
    _register(old)
    _seed_bounty(_uid("b"), old, 20, "2026-09-14T12:00:00+00:00",
                 fee=2)  # first settled 40 days before NOW
    _seed_bounty(_uid("b"), old, 20, "2026-10-24T10:00:00+00:00",
                 fee=2)  # settles today, but first is too old
    young = _uid("young")
    _register(young)
    _seed_bounty(_uid("b"), young, 20, "2026-10-14T12:00:00+00:00",
                 fee=2)  # first settled 10 days before NOW
    _seed_bounty(_uid("b"), young, 20, "2026-10-24T10:00:00+00:00",
                 fee=2)
    board = _board()
    assert _row_for(board, old) is None
    assert _row_for(board, young) is not None


def test_today_count_and_net_mix_bounty_plus_order():
    aid = _uid("mix")
    _register(aid)
    # Yesterday: first settlement (counts for eligibility, not today).
    _seed_bounty(_uid("b"), aid, 20, "2026-10-23T09:00:00+00:00", fee=2)
    # Today: one fee-recorded bounty (net 18), one fee-waived (net 20),
    # one order (amount 10, fee 1, net 9).
    _seed_bounty(_uid("b"), aid, 20, "2026-10-24T09:00:00+00:00", fee=2)
    _seed_bounty(_uid("b"), aid, 20, "2026-10-24T10:00:00+00:00",
                 fee=None, fee_waived=True)
    _seed_order(_uid("o"), aid, 10, 1, "2026-10-24T11:00:00+00:00")
    row = _row_for(_board(), aid)
    assert row is not None
    assert row["settled_today"] == 3
    assert row["net_today"] == 18 + 20 + 9
    assert row["first_settled_at"] == "2026-10-23T09:00:00+00:00"
    assert row["latest"]["kind"] == "order"
    assert row["latest"]["bounty_id"] is None
    assert row["latest"]["receipt_url"] is None
    assert row["latest"]["title"] == "NC service"
    assert row["latest"]["passport_url"] == f"/passport/{aid}"


def test_utc_day_boundary():
    aid = _uid("edge")
    _register(aid)
    _seed_bounty(_uid("b"), aid, 20, "2026-10-23T23:59:00+00:00", fee=2)
    _seed_bounty(_uid("b"), aid, 20, "2026-10-24T00:01:00+00:00", fee=2)
    row = _row_for(_board(), aid)
    assert row is not None
    assert row["settled_today"] == 1  # only the 00:01 UTC settlement
    assert row["first_settled_at"] == "2026-10-23T23:59:00+00:00"


def test_tie_breaks_net_then_first_settled():
    rich = _uid("rich")
    poor = _uid("poor")
    early = _uid("early")
    late = _uid("late")
    for aid in (rich, poor, early, late):
        _register(aid)
    # Same count (1 today), different net: higher net first.
    _seed_bounty(_uid("b"), rich, 50, "2026-10-24T09:00:00+00:00", fee=5)
    _seed_bounty(_uid("b"), poor, 20, "2026-10-24T09:00:00+00:00", fee=2)
    # Same count (1 today) and same net (18): earlier first-settled first.
    _seed_bounty(_uid("b"), early, 20, "2026-10-20T09:00:00+00:00", fee=2)
    _seed_bounty(_uid("b"), early, 20, "2026-10-24T09:00:00+00:00", fee=2)
    _seed_bounty(_uid("b"), late, 20, "2026-10-22T09:00:00+00:00", fee=2)
    _seed_bounty(_uid("b"), late, 20, "2026-10-24T09:00:00+00:00", fee=2)
    ids = [r["id"] for r in _board()["rows"]]
    assert ids.index(rich) < ids.index(poor)
    assert ids.index(early) < ids.index(late)


def test_staff_and_banned_excluded():
    _seed_bounty(_uid("b"), "dail_host", 20,
                 "2026-10-24T09:00:00+00:00", fee=2)
    banned = _uid("banned")
    _register(banned)
    dail.agents[banned].status = "banned"
    _seed_bounty(_uid("b"), banned, 20, "2026-10-24T09:00:00+00:00",
                 fee=2)
    board = _board()
    assert _row_for(board, "dail_host") is None
    assert _row_for(board, banned) is None


def test_resolved_order_needs_completed_at():
    aid = _uid("res")
    _register(aid)
    # Resolved but never completed: completed_at None -> not a settlement.
    _seed_order(_uid("o"), aid, 10, 0, None, status="resolved")
    board = _board()
    assert _row_for(board, aid) is None
    # Resolved WITH a completed_at counts.
    _seed_order(_uid("o"), aid, 10, 1, "2026-10-24T09:00:00+00:00",
                status="resolved")
    row = _row_for(_board(), aid)
    assert row is not None
    assert row["settled_today"] == 1
    assert row["net_today"] == 9


def test_no_submission_text_and_exact_row_shape():
    aid = _uid("shape")
    _register(aid)
    marker = "NC_SECRET_SUBMISSION_MARKER_XYZ"
    _seed_bounty(_uid("b"), aid, 20, "2026-10-24T09:00:00+00:00", fee=2,
                 submission=marker)
    board = _board()
    assert marker not in json.dumps(board)
    row = _row_for(board, aid)
    assert set(row.keys()) == {"id", "name", "settled_today", "net_today",
                               "first_settled_at", "latest"}
    assert set(row["latest"].keys()) == {"kind", "title", "bounty_id",
                                         "receipt_url", "passport_url"}
    assert row["latest"]["kind"] == "bounty"
    assert row["latest"]["receipt_url"] == \
        f"/receipts/{row['latest']['bounty_id']}"


def test_top_10_cap():
    # 12 fresh newcomers today join earlier tests' newcomers on the same
    # board: the board must still return exactly 10 rows.
    for i in range(12):
        aid = _uid("cap")
        _register(aid)
        _seed_bounty(_uid("b"), aid, 20, "2026-10-24T09:00:00+00:00",
                     fee=2)
    board = _board()
    assert len(board["rows"]) == 10
