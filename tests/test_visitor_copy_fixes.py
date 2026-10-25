"""Fresh-visitor UX audit copy fixes: contradictions and ordering a careful
visitor catches, fixed with words only.

These tests prove the copy is in the SERVED output (not just in the files).
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
from dail.api import app

client = TestClient(app)


def test_homepage_no_withdrawal_contradiction():
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "controlled withdrawals" not in body
    assert "never cashed out" in body


def test_homepage_no_stuck_payment_status():
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "CHECKING PAYMENT RAIL" not in body
    assert "PAYMENT RAIL STANDBY" in body


def test_homepage_plain_sentence():
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert (
        "Your agent can find work here, earn DAIL, "
        "and spend it on services from other agents." in body
    )


def test_llms_claims_lead_with_4h_clock():
    r = client.get("/llms.txt")
    assert r.status_code == 200
    body = r.text
    assert "24h to submit your work" not in body
    assert "4h anti-squat clock runs first" in body
    assert body.index("4h anti-squat clock runs first") < body.index(
        "24h work window"
    )


def test_skill_claims_lead_with_4h_clock():
    r = client.get("/skill.md")
    assert r.status_code == 200
    body = r.text
    assert "THE EFFECTIVE DEADLINE IS THE 4H ANTI-SQUAT CLOCK" in body


def test_referral_qualifier_completed_escrow_released():
    skill = client.get("/skill.md").text
    assert "first completed, escrow-released trade" in skill
    quick = client.get("/quickstart").text
    assert "first trade completes and its escrow releases" in quick


def test_quickstart_claims_lead_with_4h_clock():
    r = client.get("/quickstart")
    assert r.status_code == 200
    body = r.text
    assert "clock that matters first is the 4-hour anti-squat clock" in body
    assert body.index("clock that matters first") < body.index(
        "Reserve the bounty"
    )


def test_observatory_guarantee_covers_disputes():
    r = client.get("/observatory/public")
    assert r.status_code == 200
    body = r.text
    assert "Filing a dispute costs 1 DAIL" in body
    assert "held for admin resolution, not auto-refunded" in body
