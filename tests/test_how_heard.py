"""Registration attribution: `how_heard` free-text field is accepted, stored,
capped, and readable at the admin-only GET /admin/attribution."""
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
ADMIN = {"X-DAIL-Admin-Key": "test-admin-key"}


def _uid(prefix):
    _seq[0] += 1
    return f"{prefix}_hh{_seq[0]}"


def _register(aid, **extra):
    payload = {"id": aid, "name": aid}
    payload.update(extra)
    r = client.post("/agents", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_how_heard_stored_and_summarized():
    aid = _uid("agent")
    _register(aid, how_heard="OpenTask")
    assert dail.world_agents.how_heard.get(aid) == "OpenTask"
    r = client.get("/admin/attribution", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_answered"] >= 1
    assert body["by_source"].get("opentask", 0) >= 1  # normalized lowercase


def test_how_heard_blank_not_stored():
    aid = _uid("agent")
    _register(aid)  # no how_heard
    assert aid not in dail.world_agents.how_heard
    aid2 = _uid("agent")
    _register(aid2, how_heard="   ")
    assert aid2 not in dail.world_agents.how_heard


def test_how_heard_capped_at_200_chars():
    aid = _uid("agent")
    _register(aid, how_heard="x" * 500)
    stored = dail.world_agents.how_heard.get(aid, "")
    assert len(stored) == 200


def test_how_heard_is_attribution_only():
    # The field must not change the grant, limits, or status.
    plain = _register(_uid("agent"))
    with_field = _register(_uid("agent"), how_heard="moltbook")
    assert with_field["balance"] == plain["balance"]  # grant unchanged
    assert with_field["status"] == "active"


def test_attribution_requires_admin():
    r = client.get("/admin/attribution")
    assert r.status_code in (401, 403)
