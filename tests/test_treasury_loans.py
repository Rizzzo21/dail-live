import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)
_seq = [0]
_keys = {}

ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_tl{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]
    return _keys[aid]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def _bal(aid):
    return client.get(f"/ledger/{aid}", headers=_auth(aid)).json()["balance"]


def _treasury():
    return client.get("/treasury").json()["balance"]


def _fund_treasury(amount_needed):
    """Build treasury balance via trades (10% fee flows to treasury)."""
    a, b = _uid("tra"), _uid("trb")
    _make(a)
    _make(b)
    n = 0
    while _treasury() < amount_needed and n < 20:
        r = client.post("/world/trades", headers=_auth(a),
                        json={"seller_id": b, "buyer_id": a, "amount": 50,
                              "item": "funding", "idempotency_key": f"tfund:{a}:{n}"})
        assert r.status_code == 200, r.text
        # reverse direction so both keep spending power
        r = client.post("/world/trades", headers=_auth(b),
                        json={"seller_id": a, "buyer_id": b, "amount": 50,
                              "item": "funding", "idempotency_key": f"tfund:{b}:{n}"})
        assert r.status_code == 200, r.text
        n += 1
    assert _treasury() >= amount_needed


def test_disburse_moves_treasury_funds_without_minting():
    _fund_treasury(30)
    aid = _uid("borrower")
    _make(aid)
    t0, b0 = _treasury(), _bal(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 25, "memo": "operating credit"})
    assert r.status_code == 201, r.text
    loan = r.json()
    assert loan["agent_id"] == aid
    assert loan["principal"] == 25
    assert loan["outstanding"] == 25
    assert loan["status"] == "open"
    # No new supply: treasury down exactly 25, agent up exactly 25 (no fee skim).
    assert _treasury() == t0 - 25
    assert _bal(aid) == b0 + 25


def test_disburse_requires_admin():
    aid = _uid("borrower")
    _make(aid)
    # no key
    r = client.post("/admin/treasury/loans",
                    json={"agent_id": aid, "amount": 5})
    assert r.status_code == 403, r.text
    # wrong key
    r = client.post("/admin/treasury/loans",
                    headers={"X-DAIL-Admin-Key": "nope"},
                    json={"agent_id": aid, "amount": 5})
    assert r.status_code == 403, r.text
    # agent key is not admin
    r = client.post("/admin/treasury/loans", headers=_auth(aid),
                    json={"agent_id": aid, "amount": 5})
    assert r.status_code == 403, r.text


def test_disburse_unknown_agent_404():
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": "ghost_agent_zzz", "amount": 5})
    assert r.status_code == 404, r.text


def test_disburse_rejects_second_open_loan():
    _fund_treasury(40)
    aid = _uid("borrower")
    _make(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 10})
    assert r.status_code == 201, r.text
    # A distinct second loan (different amount => not a replay) is rejected
    # while the first is still open.
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 11})
    assert r.status_code == 400, r.text
    assert "open treasury loan" in r.text


def test_disburse_rejects_insufficient_treasury():
    aid = _uid("borrower")
    _make(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 10 ** 12})
    assert r.status_code == 400, r.text


def test_disburse_idempotent_on_retry():
    _fund_treasury(30)
    aid = _uid("borrower")
    _make(aid)
    t0 = _treasury()
    payload = {"agent_id": aid, "amount": 12, "idempotency_key": f"loanidem:{aid}"}
    r1 = client.post("/admin/treasury/loans", headers=ADMIN, json=payload)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/admin/treasury/loans", headers=ADMIN, json=payload)
    assert r2.status_code == 201, r2.text
    assert r1.json()["id"] == r2.json()["id"]
    # Treasury debited exactly once.
    assert _treasury() == t0 - 12


def test_repay_partial_and_full():
    _fund_treasury(40)
    aid = _uid("borrower")
    _make(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 20})
    loan_id = r.json()["id"]
    t0, b0 = _treasury(), _bal(aid)
    # partial
    r = client.post(f"/admin/treasury/loans/{loan_id}/repay", headers=ADMIN,
                    json={"amount": 8})
    assert r.status_code == 200, r.text
    assert r.json()["outstanding"] == 12
    assert r.json()["repaid"] == 8
    assert r.json()["status"] == "open"
    assert _treasury() == t0 + 8
    assert _bal(aid) == b0 - 8
    # full
    r = client.post(f"/admin/treasury/loans/{loan_id}/repay", headers=ADMIN,
                    json={"amount": 12})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "repaid"
    assert r.json()["outstanding"] == 0
    # overpay rejected
    r = client.post(f"/admin/treasury/loans/{loan_id}/repay", headers=ADMIN,
                    json={"amount": 1})
    assert r.status_code == 400, r.text


def test_repay_unknown_loan_404():
    r = client.post("/admin/treasury/loans/tloan_9999/repay", headers=ADMIN,
                    json={"amount": 1})
    assert r.status_code == 404, r.text


def test_repay_requires_admin():
    _fund_treasury(30)
    aid = _uid("borrower")
    _make(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 10})
    loan_id = r.json()["id"]
    r = client.post(f"/admin/treasury/loans/{loan_id}/repay",
                    json={"amount": 5})
    assert r.status_code == 403, r.text


def test_loans_list_and_treasury_receivable():
    _fund_treasury(50)
    aid = _uid("borrower")
    _make(aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 18, "memo": "ops"})
    assert r.status_code == 201, r.text
    r = client.get("/admin/treasury/loans", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    mine = [l for l in body["loans"] if l["agent_id"] == aid and l["status"] == "open"]
    assert len(mine) == 1
    assert body["loans_receivable"] >= 18
    # treasury report carries the receivable too
    rep = client.get("/treasury").json()
    assert rep["loans_receivable"] >= 18
    assert rep["open_loans"] >= 1


def test_agent_record_balance_reflects_loan():
    _fund_treasury(30)
    aid = _uid("borrower")
    _make(aid)
    before = client.get("/agents", headers=_auth(aid)).json()
    b0 = next(a["balance"] for a in before if a["id"] == aid)
    r = client.post("/admin/treasury/loans", headers=ADMIN,
                    json={"agent_id": aid, "amount": 22})
    assert r.status_code == 201, r.text
    after = client.get("/agents", headers=_auth(aid)).json()
    b1 = next(a["balance"] for a in after if a["id"] == aid)
    assert b1 == b0 + 22
