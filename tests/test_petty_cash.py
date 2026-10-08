"""Petty cash: the separate vault pot (dail:vault:petty).

- POST /admin/petty/seed — admin-only, idempotent, moves 500 existing DAIL
  from dail:vault (not a mint).
- GET /admin/petty/status — admin-only ledger view.
- POST /admin/petty/pay — admin-only, pays an agent for a task; the task
  lands in the transfer memo and the petty.pay audit event.
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


def _agent():
    aid = "petty_" + uuid.uuid4().hex[:8]
    r = client.post("/agents", json={"id": aid, "name": "Petty Tester"})
    assert r.status_code in (200, 201), r.text
    return aid


def _seed():
    r = client.post("/admin/petty/seed", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def test_petty_endpoints_require_admin_key():
    for method, url, kwargs in [
        ("POST", "/admin/petty/seed", {}),
        ("GET", "/admin/petty/status", {}),
        ("POST", "/admin/petty/pay",
         {"json": {"agent_id": "x", "amount": 5, "task": "a real task here"}}),
    ]:
        r = client.request(method, url, **kwargs)
        assert r.status_code == 403, (url, r.text)
        r = client.request(method, url, headers={"X-DAIL-Admin-Key": "wrong"},
                           **kwargs)
        assert r.status_code == 403, (url, r.text)


def test_petty_seed_idempotent():
    first = _seed()
    assert first["balance"] == 500, first
    assert first["seeded"] is True
    second = _seed()
    assert second["seeded"] is False
    assert second["balance"] == 500, second


def test_petty_pay_happy_path():
    _seed()
    aid = _agent()
    agents = {a["id"]: a for a in client.get("/agents", headers=ADMIN).json()}
    bal_before = agents[aid]["balance"]
    task = "Write the onboarding FAQ section for new agents joining DAiL"
    r = client.post("/admin/petty/pay", headers=ADMIN,
                    json={"agent_id": aid, "amount": 25, "task": task})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["to"] == aid and body["amount"] == 25
    assert body["task"] == task
    agents = {a["id"]: a for a in client.get("/agents", headers=ADMIN).json()}
    assert agents[aid]["balance"] == bal_before + 25
    # The task is in the petty ledger view.
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    assert st["balance"] == 500 - 25 - _other_payouts(st, aid, 25)
    hit = [t for t in st["recent_transfers"]
           if t["to"] == aid and t["amount"] == 25]
    assert hit and hit[0]["task"] == task, st["recent_transfers"][:3]
    assert hit[0]["kind"] == "petty_pay"


def _other_payouts(st, aid, amount):
    # Other tests in this file also pay from the shared pot; account for them.
    return sum(t["amount"] for t in st["recent_transfers"]
               if t["kind"] == "petty_pay" and not (t["to"] == aid and t["amount"] == amount))


def test_petty_pay_insufficient_funds():
    _seed()
    aid = _agent()
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    # Drain the pot to < 100, then a max-size pay must 400.
    remaining = st["balance"]
    if remaining >= 100:
        n = (remaining - 50) // 100
        for i in range(n):
            r = client.post("/admin/petty/pay", headers=ADMIN,
                            json={"agent_id": aid, "amount": 100,
                                  "task": f"Draining the pot for the insufficient-funds test (round {i})"})
            assert r.status_code == 201, r.text
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    assert st["balance"] < 100
    r = client.post("/admin/petty/pay", headers=ADMIN,
                    json={"agent_id": aid, "amount": 100,
                          "task": "This should fail with insufficient petty funds"})
    assert r.status_code == 400, r.text
    assert "insufficient" in r.text.lower()


def test_petty_pay_validation():
    _seed()
    aid = _agent()
    # amount bounds (model: 1-100)
    for bad in (0, 101):
        r = client.post("/admin/petty/pay", headers=ADMIN,
                        json={"agent_id": aid, "amount": bad,
                              "task": "A perfectly fine task description here"})
        assert r.status_code == 422, (bad, r.text)
    # task length bounds (model: 10-500)
    r = client.post("/admin/petty/pay", headers=ADMIN,
                    json={"agent_id": aid, "amount": 5, "task": "short"})
    assert r.status_code == 422, r.text
    r = client.post("/admin/petty/pay", headers=ADMIN,
                    json={"agent_id": aid, "amount": 5, "task": "x" * 501})
    assert r.status_code == 422, r.text
    # unknown agent
    r = client.post("/admin/petty/pay", headers=ADMIN,
                    json={"agent_id": "no_such_agent_xyz", "amount": 5,
                          "task": "A perfectly fine task description here"})
    assert r.status_code == 404, r.text


def test_petty_status_shape():
    _seed()
    st = client.get("/admin/petty/status", headers=ADMIN).json()
    assert st["account"] == "dail:vault:petty"
    assert st["currency"] == "DAIL"
    assert st["seed_amount"] == 500
    assert isinstance(st["balance"], int)
    assert isinstance(st["total_paid_out"], int)
    assert isinstance(st["recent_transfers"], list)
    assert len(st["recent_transfers"]) <= 20
