"""Suite-wide defaults for the DAiL test run.

The registration faucet guard reads DAIL_REG_LIMIT per request. The suite
registers far more than the production default (5/IP/day), so raise it here;
individual tests that exercise the guard lower it with monkeypatch.
"""
import os

os.environ.setdefault("DAIL_REG_LIMIT", "10000")
# The petty-cash vault funds starter grants in tests: raise the cap and keep
# the shared test app's vault topped up (individual Dail() instances in
# test_vault.py manage their own vault state).
os.environ.setdefault("DAIL_VAULT_MAX_SUPPLY", "1000000")

import pytest


@pytest.fixture(autouse=True)
def _ensure_test_vault_funded():
    from dail.api import dail as _d
    if _d.ledger.balances.get("dail:vault", 0) < 500000:
        _d.vault_mint(1000000, "test seed", idem="test-vault-seed")
    yield


def fund_vault(d, amount=100000, idem="test-vault-seed"):
    """Fund a fresh Dail() instance's vault so create_agent works in tests."""
    d.vault_mint(amount, "test seed", idem=idem)
