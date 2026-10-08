"""Petty-cash commissions: hold-and-release task payments.

- POST /admin/petty/commission — open: funds move petty -> hold.
- POST /admin/petty/commission/{id}/complete — release held funds to agent.
- POST /admin/petty/commission/{id}/cancel — return held funds to the pot.
- GET /admin/petty/status — now includes open_commissions[], held_balance,
  available_balance.
"""
import os
import sys
import uuid
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}
TASK = "Audit the bounty board for stale listings and report findings"


def _agent():
    aid = "comm_" + uuid.uuid4().hex[:8]
    r = client.post("/agents", json={"id": aid, "name": "Commission Tester"})
    assert r.status_code in (200, 201), r.text
    return aid


def _seed():
    r = client.post("/admin/petty/seed", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _balance(aid):
    agents = {a["id"]: a for a in client.get("/agents", headers=ADMIN).json()}
    return agents[aid]["balance"]


def test_commission_endpoints_require_admin_key():
    aid = _agent()
    for method, url, kwargs in [
        ("POST", "/admin/petty/commission",
         {"json": {"agent_id": aid, "amount": 5, "task": TASK}}),
        ("POST", "/admin/petty/commission/pc_0001/complete", {}),
        ("POST", "/admin/petty/commission/pc_0001/cancel", {}),
    ]:
        r = client.request(method, url, **kwargs)
        assert r.status_code == 403, (url, r.text)
        r = client.request(method, url, headers={"X-DAIL-Admin-Key": "wrong"},
                           **kwargs)
        assert r.status_code == 403, (url, r.text)


def test_commission_open_holds_funds():
    _seed()
    aid = _agent()
    before = _balance(aid)
    r = client.post("/admin/petty/commission", headers=ADMIN,
                    json={"agent_id": aid, "amount": 10, "task": TASK})
    assert r.status_code == 201, r.text
    comm = r.json()["commission"]
    assert comm["status"] == "open"
    assert comm["agent_id"] == aid and comm["amount"] == 10
    assert comm["task"] == TASK
    # Agent NOT paid yet; pot available drops; held rises.
    assert _balance(aid) == before
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    assert st["held_balance"] >= 10, st
    assert st["available_balance"] == st["balance"]
    assert any(c["id"] == comm["id"] for c in st["open_commissions"])
    assert any(c["task"] == TASK for c in st["open_commissions"])


def test_commission_complete_pays_agent():
    _seed()
    aid = _agent()
    before = _balance(aid)
    cid = client.post("/admin/petty/commission", headers=ADMIN,
                      json={"agent_id": aid, "amount": 15, "task": TASK}).json()["commission"]["id"]
    r = client.post(f"/admin/petty/commission/{cid}/complete", headers=ADMIN)
    assert r.status_code == 200, r.text
    rec = r.json()["commission"]
    assert rec["status"] == "completed"
    assert _balance(aid) == before + 15
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    assert not any(c["id"] == cid for c in st["open_commissions"])
    # Double-complete -> 400.
    r = client.post(f"/admin/petty/commission/{cid}/complete", headers=ADMIN)
    assert r.status_code == 400, r.text


def test_commission_cancel_returns_funds():
    _seed()
    aid = _agent()
    agent_before = _balance(aid)
    before_avail = client.get("/admin/petty/status", headers=ADMIN).json()["available_balance"]
    cid = client.post("/admin/petty/commission", headers=ADMIN,
                      json={"agent_id": aid, "amount": 20, "task": TASK}).json()["commission"]["id"]
    mid = client.get("/admin/petty/status", headers=ADMIN).json()
    assert mid["available_balance"] == before_avail - 20
    r = client.post(f"/admin/petty/commission/{cid}/cancel", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["commission"]["status"] == "cancelled"
    after = client.get("/admin/petty/status", headers=ADMIN).json()
    assert after["available_balance"] == before_avail
    assert _balance(aid) == agent_before  # agent never paid
    # Double-cancel -> 400.
    r = client.post(f"/admin/petty/commission/{cid}/cancel", headers=ADMIN)
    assert r.status_code == 400, r.text


def test_commission_insufficient_available():
    _seed()
    aid = _agent()
    avail = client.get("/admin/petty/status", headers=ADMIN).json()["available_balance"]
    r = client.post("/admin/petty/commission", headers=ADMIN,
                    json={"agent_id": aid, "amount": min(100, avail + 1), "task": TASK})
    # avail+1 > avail -> 400 (unless avail is 100, then amount=100 is fine;
    # guard by requesting more than available)
    if avail < 100:
        assert r.status_code == 400, r.text
    else:
        assert r.status_code == 201, r.text


def test_commission_unknown_agent_and_id():
    _seed()
    r = client.post("/admin/petty/commission", headers=ADMIN,
                    json={"agent_id": "no_such_agent_xyz", "amount": 5, "task": TASK})
    assert r.status_code == 404, r.text
    r = client.post("/admin/petty/commission/pc_9999/complete", headers=ADMIN)
    assert r.status_code == 404, r.text
    r = client.post("/admin/petty/commission/pc_9999/cancel", headers=ADMIN)
    assert r.status_code == 404, r.text


def test_commission_validation_bounds():
    _seed()
    aid = _agent()
    # amount 0 -> 422 from pydantic; task too short -> 422
    r = client.post("/admin/petty/commission", headers=ADMIN,
                    json={"agent_id": aid, "amount": 0, "task": TASK})
    assert r.status_code == 422, r.text
    r = client.post("/admin/petty/commission", headers=ADMIN,
                    json={"agent_id": aid, "amount": 5, "task": "short"})
    assert r.status_code == 422, r.text
