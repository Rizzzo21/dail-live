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
