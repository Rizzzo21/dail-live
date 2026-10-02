"""Petty-cash vault: the single authorized source of new DAIL.

- Admin mints into dail:vault up to a hard cap. No other path creates DAIL.
- Starter grants are drawn from the vault, never minted ad hoc.
- Staff (host/manager) disburse for ops within daily caps.
- Public read-only status: real numbers, always.
"""
import os
import pytest
from fastapi.testclient import TestClient

from dail import api as api_mod
from dail.ledger import Ledger, LedgerError
from dail.models import Agent
from dail.service import Dail, VAULT_ACCOUNT

client = api_mod.client if hasattr(api_mod, "client") else None


def _fresh_dail():
    # Fresh in-memory instance (no persistence): vault starts empty.
    # Isolate from any DATABASE_URL leaked by other test modules.
    for k in ("DATABASE_URL", "DAIL_REAL_PAYMENTS"):
        os.environ.pop(k, None)
    d = Dail()
    assert d.ledger.balances.get(VAULT_ACCOUNT, 0) == 0
    return d


def test_vault_mint_creates_dail_up_to_cap(monkeypatch):
    monkeypatch.setenv("DAIL_VAULT_MAX_SUPPLY", "10000")
    d = _fresh_dail()
    d.vault_mint(1000, "seed", idem="t:mint1")
    assert d.ledger.balances[VAULT_ACCOUNT] == 1000
    st = d.vault_status()
    assert st["total_minted"] == 1000
    assert st["balance"] == 1000
    # Cap enforced (10000).
    with pytest.raises(LedgerError, match="cap exceeded"):
        d.vault_mint(9500, "too much", idem="t:mint2")


def test_vault_mint_idempotent():
    d = _fresh_dail()
    d.vault_mint(500, "seed", idem="t:idem1")
    d.vault_mint(500, "seed", idem="t:idem1")  # replay: no double mint
    assert d.ledger.balances[VAULT_ACCOUNT] == 500
    assert d.vault_status()["total_minted"] == 500


def test_starter_grant_draws_from_vault():
    d = _fresh_dail()
    d.vault_mint(1000, "seed", idem="t:g1")
    agent, _key = d.create_agent(Agent(id="newbie1", name="Newbie One", goal="g", balance=100))
    assert agent.balance == 100
    assert d.ledger.balances[VAULT_ACCOUNT] == 900
    # Grant is a transfer from the vault, not a SYSTEM mint.
    txs = [t for t in d.ledger.transactions.values() if t.kind == "grant"]
    assert len(txs) == 1
    assert txs[0].from_account == VAULT_ACCOUNT


def test_registration_fails_safe_when_vault_empty():
    d = _fresh_dail()  # vault empty: no mint
    with pytest.raises(LedgerError, match="vault_empty"):
        d.create_agent(Agent(id="newbie2", name="Newbie Two", goal="g", balance=100))
    assert "newbie2" not in d.agents  # rolled back, not half-created


def test_vault_disburse_staff_only_and_capped(monkeypatch):
    d = _fresh_dail()
    d.vault_mint(1000, "seed", idem="t:d1")
    d.create_agent(Agent(id="dail_host", name="Host", goal="g", balance=0))
    monkeypatch.setenv("DAIL_VAULT_DISBURSE_DAILY_CAP", "100")
    d.vault_disburse("dail_host", "dail_host", 60, "welcomes", idem="t:dis1")
    assert d.ledger.balances["dail_host"] == 60
    # Non-staff rejected.
    with pytest.raises(LedgerError, match="not authorized"):
        d.vault_disburse("darwin142-ot", "dail_host", 10, "x", idem="t:dis2")
    # Daily cap enforced.
    with pytest.raises(LedgerError, match="daily disburse cap"):
        d.vault_disburse("dail_host", "dail_host", 50, "x", idem="t:dis3")
    # Unknown recipient.
    with pytest.raises(KeyError):
        d.vault_disburse("dail_host", "ghost", 10, "x", idem="t:dis4")


def test_vault_status_is_public():
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    r = c.get("/vault/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vault"] == "dail:vault"
    assert "balance" in body and "total_minted" in body and "max_supply" in body


def test_vault_mint_requires_admin():
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    r = c.post("/admin/vault/mint", json={"amount": 100, "reason": "x"})
    assert r.status_code == 403


def test_vault_disburse_requires_staff():
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    r = c.post("/vault/disburse",
               json={"agent_id": "dail_host", "amount": 10, "purpose": "x"})
    assert r.status_code in (401, 403)
