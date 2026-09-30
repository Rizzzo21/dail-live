"""Suite-wide defaults for the DAiL test run.

The registration faucet guard reads DAIL_REG_LIMIT per request. The suite
registers far more than the production default (5/IP/day), so raise it here;
individual tests that exercise the guard lower it with monkeypatch.
"""
import os

os.environ.setdefault("DAIL_REG_LIMIT", "10000")
