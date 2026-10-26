"""Recently-paid strip (competitor scan 2026-10-26 item 2): the last
settled bounty escrows, server-rendered on the homepage and /hire, each
linking to the hunter's passport and the signed receipt. Real ledger
numbers only: staff and probe-named hunters never appear, and an empty
wall renders the quiet state instead of invented rows.
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
from dail.api import app, dail, _recent_paid_html
from dail.models import Agent

client = TestClient(app)
_seq = [0]

# Far-future completion date: sorts above every other test's 2026 rows so
# the seeded strip rows are the ones under test, without touching any
# other test's data.
WHEN = "2999-01-02T00:00:00+00:00"


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_rp{_seq[0]}"


def _register(aid):
    if aid in dail.agents:
        return dail.agents[aid]
    agent, _key = dail.create_agent(
        Agent(id=aid, name=aid, goal="recently-paid test", balance=100))
    return agent


def _seed_completed(bid, hunter, reward, fee, title):
    dail.world_agents.bounties[bid] = {
        "id": bid, "poster_id": "rp_poster", "poster_name": "rp_poster",
        "title": title, "description": "recently-paid test bounty",
        "reward": reward, "status": "completed", "hunter_id": hunter,
        "created_at": WHEN, "completed_at": WHEN,
        "fee": fee, "fee_waived": False}
    return bid


def _seed_strip_rows():
    """One genuine external row, one staff row, one probe-named row —
    all with the same completion date so all three reach the paid wall."""
    good = _uid("goodworker")
    _register(good)
    good_bid = _seed_completed(_uid("b"), good, 20, 2,
                               "Strip test real job")
    probe = _uid("probe_worker")
    _register(probe)
    probe_bid = _seed_completed(_uid("b"), probe, 50, 5,
                                "Strip test probe job")
    staff_bid = _seed_completed(_uid("b"), "dail_host", 100, 10,
                                "Strip test staff job")
    return good, good_bid, probe_bid, staff_bid


def test_strip_shows_settled_escrow_with_receipt_and_passport_links():
    good, good_bid, _probe_bid, _staff_bid = _seed_strip_rows()
    html = _recent_paid_html()
    assert "RECENTLY PAID" in html
    assert "Strip test real job" in html
    assert "18 DAIL" in html  # net of the recorded 2-DAIL fee on 20
    assert f"/passport/{good}" in html
    assert f"/receipts/{good_bid}" in html


def test_strip_excludes_staff_and_probe_rows():
    _good, _good_bid, probe_bid, staff_bid = _seed_strip_rows()
    html = _recent_paid_html()
    assert "Strip test staff job" not in html
    assert "Strip test probe job" not in html
    assert f"/receipts/{staff_bid}" not in html
    assert f"/receipts/{probe_bid}" not in html


def test_strip_renders_on_homepage_and_hire_without_javascript():
    _seed_strip_rows()
    for path in ("/", "/hire"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "RECENTLY PAID" in r.text, path
        assert "Strip test real job" in r.text, path
        assert "Strip test staff job" not in r.text, path
        assert "Strip test probe job" not in r.text, path
    assert "<script" not in _recent_paid_html()


def test_strip_empty_state_shows_quiet_text_not_invented_rows(monkeypatch):
    monkeypatch.setattr(dail, "public_observatory",
                        lambda: {"paid_wall": []})
    html = _recent_paid_html()
    assert "No settled payments yet" in html
    assert "RECENTLY PAID" not in html  # quiet state, no payment rows
    assert "Receipt" not in html
