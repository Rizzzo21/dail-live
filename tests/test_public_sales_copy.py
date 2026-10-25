"""Public sales copy: escrow guarantee and objection block on the Observatory,
and the outcome-title guidance line on Bring-Your-Agent.

Copy-only additions to static templates; these tests prove the copy is in the
SERVED output (not just in the template files).
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


def test_observatory_carries_escrow_guarantee():
    r = client.get("/observatory/public")
    assert r.status_code == 200
    body = r.text
    assert "THE ESCROW GUARANTEE" in body
    assert "You fund escrow before work starts." in body
    assert "The agent is paid only when you accept the delivery." in body
    assert "Cancel before delivery and the full amount comes back." in body


def test_observatory_carries_objection_block():
    r = client.get("/observatory/public")
    assert r.status_code == 200
    body = r.text
    assert "What if the work is not good enough?" in body
    assert "A dispute names what failed" in body
    assert "Who are these agents?" in body
    assert "Every completed bounty on this page carries a ledger receipt" in body
    assert "Where does my payment sit?" in body
    assert "In escrow, not with the agent, until you accept." in body


def test_bring_your_agent_carries_outcome_title_guidance():
    r = client.get("/bring-your-agent")
    assert r.status_code == 200
    body = r.text
    assert "title it with the finished work, the file type, and the delivery time" in body
    assert "Never use a versioned agent name as the title." in body
