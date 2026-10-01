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
                    json={"agent_id": hunter, "submission": "done"})
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
