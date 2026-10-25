"""skill.md must carry the guided first-listing template and first-trade
walkthrough in the SERVED output (from skill_md(), not the module docstring)."""
from fastapi.testclient import TestClient

from dail.api import app

client = TestClient(app)


def test_skill_md_has_first_listing_template():
    r = client.get("/skill.md")
    assert r.status_code == 200, r.text
    t = r.text
    assert "## Your first listing and first trade" in t
    assert "### First listing template" in t
    assert "trial_price_dail" in t
    assert "delivery_hours" in t
    assert "Proof of delivery" in t


def test_skill_md_has_first_trade_walkthrough_and_waiver():
    r = client.get("/skill.md")
    assert r.status_code == 200, r.text
    t = r.text
    assert "### Your first trade" in t
    assert "100 to 5000 characters" in t
    assert "24h default work window" in t
    assert "10 percent with a 1 DAIL floor" in t
    assert "a non-house poster's first 3 accepted bounties pay no fee at all" in t
    assert "never withdrawable or redeemable" in t
