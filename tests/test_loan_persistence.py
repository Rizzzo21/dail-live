"""Treasury-loan registry must survive restarts.

The ledger tx log always survived (balances replay); the loan registry did
not, which would have zeroed loans_receivable and broken repay() after any
redeploy. These tests cover: write-through persistence, restore into a fresh
service, and the ledger-rebuild migration for pre-persistence loans.
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DAIL_USDC_TREASURY", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}
_seq = [0]


def _fresh_db(tmp_path, name):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path / name}"
    for mod in [m for m in list(sys.modules) if m.startswith("dail.")]:
        del sys.modules[mod]
    from dail.service import Dail
    return Dail()


def _uid(p):
    _seq[0] += 1
    return f"{p}_lp{_seq[0]}"


def _fund(d, borrower_id, staff_id="staffer"):
    """Give the borrower a balance and the treasury enough to lend."""
    from dail.models import Agent
    for aid, bal in ((borrower_id, 100), (staff_id, 2000)):
        if aid not in d.agents:
            d.create_agent(Agent(id=aid, name=aid, goal="", balance=bal))
    # Top up treasury via a trade fee: staff buys from borrower for 1000 -> 100 fee.
    d.world_agents.trade(seller_id=borrower_id, buyer_id=staff_id, amount=1000,
            item="fund", idem=f"fund:{borrower_id}:{_seq[0]}")


def test_loans_survive_restart(tmp_path):
    d = _fresh_db(tmp_path, "loans1.db")
    borrower = _uid("bor")
    _fund(d, borrower)
    loan = d.world_agents.treasury_loan_disburse(borrower, 40, memo="ops", idem="idem:1")
    assert loan["id"] == "tloan_0001"
    d.world_agents.treasury_loan_repay(loan["id"], 15, idem="repay:1")

    d2 = _fresh_db(tmp_path, "loans1.db")  # same DB file -> restore path
    assert len(d2.world_agents.treasury_loans) == 1
    rl = d2.world_agents.treasury_loans["tloan_0001"]
    assert rl["agent_id"] == borrower
    assert rl["principal"] == 40 and rl["repaid"] == 15 and rl["outstanding"] == 25
    assert rl["status"] == "open"
    assert d2.world_agents.treasury_loans_list()["loans_receivable"] == 25
    # Repay works after restore, and seq does not collide.
    d2.world_agents.treasury_loan_repay("tloan_0001", 25, idem="repay:2")
    assert d2.world_agents.treasury_loans["tloan_0001"]["status"] == "repaid"
    b2 = _uid("bor2")
    _fund(d2, b2)
    loan2 = d2.world_agents.treasury_loan_disburse(b2, 10, idem="idem:2")
    assert loan2["id"] == "tloan_0002"


def test_rebuild_from_ledger_migration(tmp_path):
    """Loans disbursed before registry persistence existed rebuild from txs."""
    d = _fresh_db(tmp_path, "loans2.db")
    b1, b2 = _uid("mig1"), _uid("mig2")
    _fund(d, b1)
    _fund(d, b2)
    # Disburse + partial repay, then wipe the registry as if pre-persistence.
    l1 = d.world_agents.treasury_loan_disburse(b1, 48, idem="mig:a")
    d.world_agents.treasury_loan_repay(l1["id"], 8, idem="mig:r")
    l2 = d.world_agents.treasury_loan_disburse(b2, 30, idem="mig:b")
    # Wipe the registry row to simulate a pre-persistence world.
    with d.store.engine.begin() as c:
        from sqlalchemy import text
        c.execute(text("DELETE FROM dail_kv WHERE key='treasury_loans'"))

    d2 = _fresh_db(tmp_path, "loans2.db")
    assert len(d2.world_agents.treasury_loans) == 2
    r1 = d2.world_agents.treasury_loans["tloan_0001"]
    r2 = d2.world_agents.treasury_loans["tloan_0002"]
    assert (r1["agent_id"], r1["principal"], r1["repaid"], r1["outstanding"]) == (b1, 48, 8, 40)
    assert (r2["agent_id"], r2["principal"], r2["outstanding"], r2["status"]) == (b2, 30, 30, "open")
    assert d2.world_agents.treasury_loan_seq == 2
    assert d2.world_agents.treasury_loans_list()["loans_receivable"] == 70


def test_no_loans_no_crash(tmp_path):
    d = _fresh_db(tmp_path, "loans3.db")
    d2 = _fresh_db(tmp_path, "loans3.db")
    assert d2.world_agents.treasury_loans == {}
    assert d2.world_agents.treasury_loans_list() == {"loans": [], "loans_receivable": 0}
