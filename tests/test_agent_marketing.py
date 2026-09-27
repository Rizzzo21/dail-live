import sys
import os
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)


def _make_agent(aid, balance=100):
    r = client.post("/agents", json={"id": aid, "name": aid, "balance": balance})
    assert r.status_code == 200, r.text


def _balance(aid):
    return client.get(f"/ledger/{aid}").json()["balance"]


def test_payments_info_is_agent_readable():
    r = client.get("/payments/info")
    assert r.status_code == 200
    info = r.json()
    assert info["rail"] == "stripe"
    assert info["live"] is False
    assert any("checkout" in s for s in info["how_to_top_up"])
    assert any("bulletins" in s for s in info["how_to_earn_from_agents"])


def test_bulletin_flow_with_service_link():
    _make_agent("seller_b")
    s = client.post("/world/services", json={
        "provider_id": "seller_b", "name": "Data cleanup",
        "description": "I clean messy CSVs fast", "price": 10}).json()
    r = client.post("/world/bulletins", json={
        "agent_id": "seller_b", "title": "CSVs cleaned!",
        "body": "Fast, cheap CSV cleanup. See my service.",
        "service_id": s["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["service_id"] == s["id"]
    assert _balance("seller_b") == 95  # 5 DAIL bulletin fee
    blts = client.get("/world/bulletins").json()["bulletins"]
    assert any(b["id"] == r.json()["id"] for b in blts)


def test_bulletin_insufficient_funds():
    _make_agent("broke_b", balance=0)
    r = client.post("/world/bulletins", json={
        "agent_id": "broke_b", "title": "Hi", "body": "No funds for this"})
    assert r.status_code == 400


def test_bulletin_validation():
    _make_agent("picky_b")
    assert client.post("/world/bulletins", json={
        "agent_id": "picky_b", "title": "", "body": "x"}).status_code == 400
    assert client.post("/world/bulletins", json={
        "agent_id": "picky_b", "title": "t", "body": "b",
        "service_id": "svc_9999"}).status_code == 404
    _make_agent("other_b")
    s = client.post("/world/services", json={
        "provider_id": "other_b", "name": "S", "description": "d",
        "price": 5}).json()
    # Can't advertise someone else's service.
    r = client.post("/world/bulletins", json={
        "agent_id": "picky_b", "title": "t", "body": "b",
        "service_id": s["id"]})
    assert r.status_code == 403


def test_announce_requires_admin_key():
    r = client.post("/payments/announce")
    assert r.status_code == 403
    r = client.post("/payments/announce",
                    headers={"X-DAIL-Admin-Key": "wrong"})
    assert r.status_code == 403
