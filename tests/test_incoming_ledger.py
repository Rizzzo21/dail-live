"""Incoming-funds reconciliation ledger (admin-only)."""
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
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def test_incoming_requires_admin():
    r = client.get("/admin/payments/incoming")
    assert r.status_code == 403


def test_incoming_structure_empty_rails():
    r = client.get("/admin/payments/incoming", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usdc"]["deposits"] == []
    assert body["usdc"]["total_credited_dail"] == 0
    assert body["stripe"]["payments"] == []
    assert body["stripe"]["total_paid_usd_cents"] == 0
    assert body["stripe"]["total_credited_dail"] == 0
    assert "econcile" in body["note"]


def test_head_mirrors_get_on_public_paths():
    for path in ["/health", "/quickstart", "/llms.txt", "/openapi.json",
                 "/treasury", "/robots.txt", "/.well-known/agent-card.json"]:
        r = client.head(path)
        assert r.status_code == 200, (path, r.status_code)


def test_head_still_gated_on_private_paths():
    r = client.head("/agents")
    assert r.status_code in (401, 403)


def test_robots_txt():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    body = r.text
    assert "User-agent: *" in body
    assert "Disallow: /admin/" in body
    assert "Allow: /observatory/public" in body
    assert "llms.txt" in body
