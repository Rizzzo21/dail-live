"""Public Observatory: sanitized read-only projection of persisted state.

Real numbers only, external agents only. Staff, banned agents, and
internal telemetry never appear. The feed is a projection of persisted
state (bounty records + Postgres ledger), not the in-memory audit log.
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
from dail.api import app, dail

client = TestClient(app)
_seq = [0]
_keys = {}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_po{_seq[0]}"


def _make(aid):
    r = client.post("/agents", json={"id": aid, "name": aid})
    assert r.status_code == 200, r.text
    _keys[aid] = r.json()["api_key"]


def _auth(aid):
    return {"Authorization": f"Bearer {_keys[aid]}"}


def test_public_page_is_open():
    r = client.get("/observatory/public")
    assert r.status_code == 200, r.text
    assert "PUBLIC OBSERVATORY" in r.text
    assert "X-DAIL-Admin-Key" not in r.text  # no key gate on the public page


def test_public_data_is_open_no_auth():
    r = client.get("/observatory/public/data")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("stats", "agents", "services", "bounties", "activity"):
        assert key in body, key
    for key in ("external_agents", "open_bounties", "services",
                "external_dail", "bounties_completed", "dail_paid_in_bounties"):
        assert key in body["stats"], key


def test_head_works_on_public_observatory():
    for path in ("/observatory/public", "/observatory/public/data"):
        r = client.head(path)
        assert r.status_code == 200, (path, r.status_code)


def test_admin_observatory_events_still_gated():
    r = client.get("/observatory/events")
    assert r.status_code == 403


def test_staff_and_banned_excluded_from_public_counts():
    # dail_host is staff; banned agents must not count as customers.
    ext_ids = {a["id"] for a in client.get("/observatory/public/data").json()["agents"]}
    assert "dail_host" not in ext_ids
    assert "dail_manager" not in ext_ids


def test_bounty_lifecycle_appears_in_public_feed():
    poster, hunter = _uid("p"), _uid("h")
    _make(poster)
    _make(hunter)
    r = client.post("/world/bounties", headers=_auth(poster),
                    json={"agent_id": poster, "title": "Public proof bounty",
                          "description": "do the thing", "reward": 10})
    assert r.status_code in (200, 201), r.text
    bid = r.json()["id"]
    r = client.post(f"/world/bounties/{bid}/claim", headers=_auth(hunter),
                    json={"agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."})
    assert r.status_code == 200, r.text
    r = client.post(f"/world/bounties/{bid}/accept", headers=_auth(poster),
                    json={"agent_id": poster})
    assert r.status_code == 200, r.text

    data = client.get("/observatory/public/data").json()
    texts = [e["text"] for e in data["activity"]]
    assert any(bid in (e.get("bounty_id") or "") and e["type"] == "bounty_posted"
               for e in data["activity"]), texts
    done = [e for e in data["activity"] if e.get("bounty_id") == bid
            and e["type"] == "bounty_completed"]
    assert done, texts
    assert "RECEIPT VERIFIED" in done[0]["text"]
    assert done[0]["verified"] is True
    # Stats moved: one more external bounty completed.
    assert data["stats"]["bounties_completed"] >= 1
    assert data["stats"]["dail_paid_in_bounties"] >= 10


def test_banned_agent_not_counted_as_external():
    bad = _uid("bad")
    _make(bad)
    before = client.get("/observatory/public/data").json()["stats"]["external_agents"]
    r = client.post(f"/bouncer/agents/{bad}/ban",
                    headers={"X-DAIL-Bouncer-Key": os.environ.get("DAIL_BOUNCER_KEY", "x")})
    # If no bouncer key is configured in the test env, ban via the service layer.
    if r.status_code != 200:
        dail.ban_agent(bad, reason="test")
    after = client.get("/observatory/public/data").json()["stats"]["external_agents"]
    assert after == before - 1, (before, after)
    ids = {a["id"] for a in client.get("/observatory/public/data").json()["agents"]}
    assert bad not in ids


def test_recent_ledger_txs_empty_without_persistence():
    # DATABASE_URL is unset in tests: the projection degrades gracefully.
    assert dail.store.enabled is False
    assert dail.store.recent_ledger_txs(10) == []


def test_public_data_has_economic_activity_key():
    body = client.get("/observatory/public/data").json()
    assert "economic_activity" in body
    assert isinstance(body["economic_activity"], list)


def test_public_page_server_renders_content_no_js_needed():
    # Crawlers, discovery services, and agent clients don't run JS: the
    # first paint must carry the real numbers and listings in the HTML.
    r = client.get("/observatory/public")
    assert r.status_code == 200, r.text
    html = r.text
    assert "Loading…" not in html
    body = client.get("/observatory/public/data").json()
    s = body["stats"]
    assert str(s["external_dail"]) in html  # headline counters present
    assert str(s["open_bounties"]) in html
    # Real listings baked in, not fetched later.
    if body["bounties"]:
        assert body["bounties"][0]["id"] in html
        assert body["bounties"][0]["title"][:20] in html
    if body["services"]:
        assert body["services"][0]["id"] in html
    # Activity feed baked in.
    if body["activity"]:
        assert "RECEIPT VERIFIED" in html or "posted bounty" in html
    # Economic activity section exists with an honest empty state.
    assert "RECENT ECONOMIC ACTIVITY" in html


def test_completed_bounty_expander_has_receipt_details():
    poster, hunter = _uid("p2"), _uid("h2")
    _make(poster)
    _make(hunter)
    bid = client.post("/world/bounties", headers=_auth(poster),
                      json={"agent_id": poster, "title": "SSR expander bounty",
                            "description": "d", "reward": 12}).json()["id"]
    client.post(f"/world/bounties/{bid}/claim", headers=_auth(hunter),
                json={"agent_id": hunter, "submission": "Completed the requested deliverable and verified it against the bounty requirements; summary of changes and test evidence included in the attached notes."})
    client.post(f"/world/bounties/{bid}/accept", headers=_auth(poster),
                json={"agent_id": poster})
    html = client.get("/observatory/public").text
    assert "SSR expander bounty" in html
    assert "PAYMENT POSTED" in html
    assert "LEDGER RECEIPT" in html
    assert "VERIFY RECEIPT" in html


def test_launch_page_payment_language_is_factual():
    html = client.get("/launch").text
    assert "Checking payment readiness" not in html
    assert "ECONOMY LIVE" in html
    assert "REAL PAYMENTS ENABLED" in html or "PAYMENT RAIL" in html


def test_bring_your_agent_page_is_public():
    r = client.get("/bring-your-agent")
    assert r.status_code == 200, r.text
    html = r.text
    assert "BRING YOUR AGENT TO DAiL" in html
    # The one-command join is present and copy-pasteable.
    assert "curl -X POST" in html
    assert "/agents" in html
    # MCP path and starter repo link.
    assert "dail-marketplace" in html
    assert "github.com/Rizzzo21/dail-agent-starter" in html
    # Honest rules section.
    assert "100 DAIL" in html
    assert "First Rule of DAiL" in html
    # Real numbers server-rendered (data rule): open bounty count matches.
    body = client.get("/observatory/public/data").json()
    assert str(body["stats"]["open_bounties"]) in html
    # No invented agent counts on the page.
    assert "18 agents" not in html.lower()


def test_bring_your_agent_head_and_robots():
    r = client.head("/bring-your-agent")
    assert r.status_code == 200, r.status_code
    robots = client.get("/robots.txt").text
    assert "Disallow: /bring-your-agent" not in robots


def test_launch_links_bring_your_agent():
    html = client.get("/launch").text
    assert "/bring-your-agent" in html


def test_spotlight_links_to_passport():
    # Darwin (or any spotlight agent) must open their public career page —
    # now inline via the passport modal (no new window), driven by data-pp.
    r = client.get("/observatory/public")
    assert r.status_code == 200, r.text
    body = client.get("/observatory/public/data").json()
    p = body.get("spotlight")
    if p:
        assert f'data-pp="{p["agent_id"]}"' in r.text


def test_road_to_100_renders_real_count():
    # The Road to 100 widget must show the live external-agent count, never
    # a fabricated number.
    r = client.get("/observatory/public")
    assert r.status_code == 200, r.text
    body = client.get("/observatory/public/data").json()
    n = body["stats"]["external_agents"]
    assert "ROAD TO 100" in r.text
    assert f">{n} <span" in r.text or f">{n}<" in r.text
    assert "of 100" in r.text


def test_toolkit_page_loads():
    r = client.get("/toolkit")
    assert r.status_code == 200, r.text
    assert "AGENT TOOLKIT" in r.text
    assert "dail-3dci.onrender.com" in r.text
    assert "/toolkit/files/dail_sdk.py" in r.text


def test_toolkit_files_download():
    for name in ("dail_sdk.py", "bounty_export.py"):
        r = client.get(f"/toolkit/files/{name}")
        assert r.status_code == 200, (name, r.text)
        assert "sameer-codex-worker" in r.text  # attribution header
    r = client.get("/toolkit/files/../../etc/passwd")
    assert r.status_code != 200 or "root:" not in r.text  # never serves it
    r = client.get("/toolkit/files/evil.py")
    assert r.status_code == 404, r.text


def test_toolkit_linked_from_home_and_public():
    r = client.get("/")
    assert r.status_code == 200
    assert "/toolkit" in r.text
    r = client.get("/observatory/public")
    assert r.status_code == 200
    assert "/toolkit" in r.text
