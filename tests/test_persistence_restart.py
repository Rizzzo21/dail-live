"""Restart round-trip: services and bulletins must survive a process restart.

Uses a throwaway SQLite database as DATABASE_URL and constructs two
separate Dail instances against it (simulating a redeploy).
"""
import os
import sys
from pathlib import Path

os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import fund_vault


def test_service_and_bulletin_survive_restart(tmp_path):
    from dail.models import Agent
    from dail.service import Dail

    db_url = f"sqlite:///{tmp_path}/restart.db"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d1 = Dail()
        fund_vault(d1)
        _, raw_key = d1.create_agent(Agent(id="m1", name="m1", goal="test", balance=100))
        assert raw_key.startswith("dail_sk_")
        assert d1.keystore.verify(raw_key) == "m1"
        svc = d1.world_agents.create_service("m1", "Research brief", "desc", 25)
        assert svc["id"] == "svc_0001"
        blt = d1.world_agents.post_bulletin("m1", "title", "body", service_id="svc_0001")
        assert blt["id"] == "blt_0001"

        # "restart": brand-new instance against the same database
        d2 = Dail()
        assert "svc_0001" in d2.world_agents.services
        assert d2.world_agents.services["svc_0001"]["name"] == "Research brief"
        assert "blt_0001" in d2.world_agents.bulletins
        # agent API keys survive the restart too (hashes in Postgres)
        assert d2.keystore.verify(raw_key) == "m1"
        # sequence counters restored: next ids continue, no collisions
        svc2 = d2.world_agents.create_service("m1", "Second", "desc", 10)
        assert svc2["id"] == "svc_0002"
        blt2 = d2.world_agents.post_bulletin("m1", "t2", "b2")
        assert blt2["id"] == "blt_0002"
        # discovery sees the restored service
        found = d2.world_agents.discover("m1", "research")
        assert any(s["id"] == "svc_0001" for s in found["services"])
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def test_balances_and_identities_survive_restart(tmp_path):
    """Money-critical restart round-trip: agent identities, balances, and
    escrow must survive a process restart via the Postgres write-through log.
    """
    from dail.models import Agent
    from dail.service import Dail

    db_url = f"sqlite:///{tmp_path}/money_restart.db"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        d1 = Dail()
        fund_vault(d1)
        _, k1 = d1.create_agent(Agent(id="r1", name="Rich", goal="g", balance=100))
        _, k2 = d1.create_agent(Agent(id="r2", name="Poor", goal="g", balance=100))
        # real economic activity: a trade and a bounty escrow
        d1.world_agents.trade("r1", "r2", 40, "widget", idem="rt-1")
        b = d1.world_agents.post_bounty("r1", "Work", "do it", 30)
        # r1 sold (netted 36 after fee) and escrowed a 30 bounty; r2 bought
        assert d1.ledger.balances["r1"] == 100 + 36 - 30
        assert d1.ledger.balances["r2"] == 100 - 40

        # "restart": brand-new instance against the same database
        d2 = Dail()
        # identities restored
        assert "r1" in d2.agents and d2.agents["r1"].name == "Rich"
        assert "r2" in d2.agents
        # API keys still work
        assert d2.keystore.verify(k1) == "r1"
        assert d2.keystore.verify(k2) == "r2"
        # balances rebuilt by replaying the persisted tx log
        assert d2.ledger.balances["r1"] == d1.ledger.balances["r1"]
        assert d2.ledger.balances["r2"] == d1.ledger.balances["r2"]
        assert d2.agents["r1"].balance == d1.ledger.balances["r1"]
        # bounty escrow intact
        assert d2.world_agents.bounties[b["id"]]["status"] == "open"
        assert d2.ledger.balances[f"escrow:{b['id']}"] == 30
        # idempotency survives: replaying the trade is a no-op, not a double-spend
        d2.world_agents.trade("r1", "r2", 40, "widget", idem="rt-1")
        assert d2.ledger.balances["r1"] == d1.ledger.balances["r1"]
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
