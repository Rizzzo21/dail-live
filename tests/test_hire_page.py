"""Public buyer landing (/hire): public, server-rendered, no JavaScript,
no internal fields. It surfaces the existing invite/dashboard/Stripe/task
flow — it must not invent a payment path or leak anything the public
Observatory does not already show.
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
from dail.api import app, _PUBLIC_GET, _hire_stats_html

client = TestClient(app)


def test_hire_is_public_and_renders_steps_and_pricing():
    r = client.get("/hire")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "HOW IT WORKS" in r.text
    assert "Top up with a card" in r.text
    assert "$1 = 1 DAIL" in r.text
    assert "first 3 accepted jobs pay no house fee" in r.text
    assert "never cashed out" in r.text
    assert "7 days" in r.text
    assert "/dashboard" in r.text
    assert "/request-bounty" in r.text


def test_hire_has_no_javascript_and_no_internal_fields():
    r = client.get("/hire")
    assert r.status_code == 200
    assert "<script" not in r.text
    for leaked in ("lapsed_hunters", "rejected_hunters", "failed_claims",
                   "fee_waived", "escrow:", "not_your_agent", "X-DAIL"):
        assert leaked not in r.text


def test_hire_stats_strip_renders_live_numbers():
    html = _hire_stats_html()
    assert "open bounties" in html
    assert "DAIL paid out" in html
    assert "live record" in html


def test_hire_is_in_public_allowlist():
    assert "/hire" in _PUBLIC_GET


def test_homepage_links_to_hire():
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/hire"' in r.text


def test_robots_allows_hire():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Allow: /hire" in r.text
