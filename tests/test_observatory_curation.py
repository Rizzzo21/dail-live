"""Public Observatory services-table curation (fresh-visitor UX audit
finding #3, 2026-10-24).

The public table hides staff/banned/test rows, probe-named rows,
placeholder names, absurd legacy prices, and description-empty rows —
while /world/services stays complete. Every rule branch is tested below,
including the real listings that must STAY (description-only "probe").
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
from dail.api import app, dail, SERVICE_PRICE_CAP_DAIL
from dail.models import Agent
from dail.service import public_service_visible

client = TestClient(app)

EXT = {"ext_agent", "ext_agent_2"}
STAFF = "dail_host"


def _svc(sid, provider, name, price, desc):
    return {"id": sid, "provider_id": provider, "name": name,
            "price": price, "description": desc, "active": True}


def _base(**kw):
    d = {"id": "cur_base", "provider_id": "ext_agent",
         "name": "Real audit service", "price": 25,
         "description": "Deep security audits for agent code", "active": True}
    d.update(kw)
    return d


# --- unit: each hiding clause ---

def test_staff_provider_hidden():
    assert public_service_visible(_base(provider_id=STAFF), EXT) is False


def test_banned_or_unknown_provider_hidden():
    assert public_service_visible(_base(provider_id="banned_x"), EXT) is False
    assert public_service_visible(_base(provider_id="svcBuyer"), EXT) is False


def test_probe_in_name_hidden_case_insensitive():
    for name in ("Doc-audit probe (not for sale)", "PROBE big int price",
                 "probe-none-7481", "Dispute-flow probe service",
                 "secprobe-escrow-test", "Audit probe listing"):
        assert public_service_visible(_base(name=name), EXT) is False, name


def test_selftest_in_name_hidden():
    for name in ("SelfTest", "SelfTest2", "Self-Test probe", "selftest"):
        assert public_service_visible(_base(name=name), EXT) is False, name


def test_description_only_probe_stays():
    # Real security-audit listings say "probe" in the description only.
    for name, desc in (
            ("API Security & OpenAPI 3.0 Specification Audit",
             "End-to-end REST API security probe (IDOR, auth header binding)"),
            ("Non-destructive API security review",
             "You give me a public API base URL and scope; I probe auth"),
            ("Icon-pack license map JSON",
             "Take the free 15-pack catalog at mmprobe-openapi-lint.netlify")):
        assert public_service_visible(
            _base(name=name, description=desc), EXT) is True, name


def test_placeholder_names_hidden():
    for name in ("n", " ", "x"):
        assert public_service_visible(_base(name=name), EXT) is False, name
    assert public_service_visible(_base(name="x" * 201), EXT) is False
    assert public_service_visible(_base(name="x" * 200), EXT) is True


def test_absurd_price_hidden_at_cap_boundary():
    assert SERVICE_PRICE_CAP_DAIL == 10000
    assert public_service_visible(_base(price=10000), EXT) is True
    assert public_service_visible(_base(price=10001), EXT) is False
    assert public_service_visible(_base(price=1000000000000), EXT) is False
    assert public_service_visible(
        _base(price=9223372036854775808), EXT) is False


def test_description_empty_hidden():
    for desc in ("", " ", "d"):
        assert public_service_visible(_base(description=desc), EXT) is False, desc
    assert public_service_visible(_base(description="ok"), EXT) is True


def test_duplicate_titles_stay():
    # Dedupe is audit #18, a separate decision — not this rule.
    a = _base(id="cur_dup_a", name="Solidity audit + Foundry POC")
    b = _base(id="cur_dup_b", name="Solidity audit + Foundry POC")
    assert public_service_visible(a, EXT) is True
    assert public_service_visible(b, EXT) is True


def test_edge_bounds_names_stay():
    # Test-shaped in principle, but ordinary words that could title a
    # real listing — no rule above catches them, by design.
    assert public_service_visible(
        _base(name="edge", description="edge"), EXT) is True


# --- integration: shaping + API untouched ---

_SEEDED = []


def _register(aid):
    if aid in dail.agents:
        return
    dail.create_agent(Agent(id=aid, name=aid, goal="curation test",
                            balance=100))


def _seed(sid, provider, name, price, desc, active=True):
    dail.world_agents.services[sid] = {
        "id": sid, "provider_id": provider, "name": name, "price": price,
        "description": desc, "active": active}
    _SEEDED.append(sid)


def teardown_function():
    for sid in _SEEDED:
        dail.world_agents.services.pop(sid, None)
    _SEEDED.clear()
    for aid in ("cur_ext", "cur_ext2"):
        dail.agents.pop(aid, None)


def test_public_payload_curates_but_api_stays_complete():
    _register("cur_ext")
    _register("cur_ext2")
    _seed("cur_vis", "cur_ext", "Curated visible audit", 25,
          "Deep security audits for agent code")
    _seed("cur_probe", "cur_ext", "Doc-audit probe (not for sale)", 1,
          "Onboarding documentation audit probe listing")
    _seed("cur_staff", "dail_host", "Staff research brief", 25,
          "A real staff listing, hidden from the public table only")
    _seed("cur_absurd", "cur_ext2", "bounds", 1000000000000, "z")
    _seed("cur_tiny", "cur_ext", "n", 1, "d")
    _seed("cur_desc_probe", "cur_ext2", "API Security Audit", 8,
          "End-to-end REST API security probe (IDOR checks)")
    _seed("cur_inactive", "cur_ext", "Inactive real listing", 10,
          "Should stay out via the active filter", active=False)

    data = dail.public_observatory()
    ids = [s["id"] for s in data["services"]]
    assert "cur_vis" in ids
    assert "cur_desc_probe" in ids  # description-only probe stays
    for hidden in ("cur_probe", "cur_staff", "cur_absurd", "cur_tiny",
                   "cur_inactive"):
        assert hidden not in ids, hidden
    assert data["stats"]["services"] == len(ids)

    # The API record is untouched: hidden rows are still listed there.
    resp = client.get("/world/services")
    assert resp.status_code == 200
    api_ids = [s["id"] for s in
               (resp.json() if isinstance(resp.json(), list)
                else resp.json()["services"])]
    for sid in ("cur_vis", "cur_probe", "cur_staff", "cur_absurd",
                "cur_tiny"):
        assert sid in api_ids, sid


def test_public_page_footnote_and_rows():
    _register("cur_ext")
    _seed("cur_vis2", "cur_ext", "Curated visible audit two", 25,
          "Deep security audits for agent code")
    _seed("cur_probe2", "cur_ext", "Doc-audit probe (not for sale)", 1,
          "Onboarding documentation audit probe listing")
    html = client.get("/observatory/public").text
    assert "exclude staff listings, test probes, and placeholder rows" in html
    assert "Curated visible audit two" in html
    assert "Doc-audit probe (not for sale)" not in html
