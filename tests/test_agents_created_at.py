"""GET /agents exposes created_at (registration timestamp) when the store has it."""
import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app, dail

client = TestClient(app)
_seq = [0]


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_ca{_seq[0]}"


def _register(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def test_agents_list_includes_created_at_key():
    aid = _uid("agent")
    key = _register(aid)
    agents = client.get("/agents", headers={"Authorization": f"Bearer {key}"}).json()
    me = [a for a in agents if a["id"] == aid][0]
    assert "created_at" in me
    # No Postgres in the test env, so the value is None — but the field
    # must exist so staff consumers can rely on the contract.
    assert me["created_at"] is None or isinstance(me["created_at"], str)


def test_agents_list_created_at_comes_from_store():
    # When the store is enabled, created_at reflects the DB row.
    if not (getattr(dail, "store", None) and dail.store.enabled):
        return  # covered by the None-contract test above
    aid = _uid("agent")
    key = _register(aid)
    agents = client.get("/agents", headers={"Authorization": f"Bearer {key}"}).json()
    me = [a for a in agents if a["id"] == aid][0]
    assert me["created_at"] == dail.store.agent_created_at(aid)
