"""USDC on-ramp (Base) rail tests. The chain is mocked at the _rpc seam:
no test here touches a real RPC endpoint."""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
os.environ["DAIL_USDC_MIN_CONF"] = "2"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TREASURY = "0xCd787bCf82279c121835EaaB37b34A502A6b8dBC"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA4a1309"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FAKE_USDC = "0xdead0000000000000000000000000000000000beef"
PAYER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def _db(tmp_path, name):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path / name}"


def _pad(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def _receipt(to=TREASURY, units=25_000000, contract=USDC, status="0x1", block="0x100"):
    return {
        "status": status,
        "blockNumber": block,
        "logs": [{
            "address": contract,
            "topics": [TRANSFER_TOPIC, _pad(PAYER), _pad(to)],
            "data": hex(units),
        }],
    }


def _rpc_factory(receipt=None, latest="0x110"):
    def fake(method, params):
        if method == "eth_getTransactionReceipt":
            return receipt
        if method == "eth_blockNumber":
            return latest
        raise AssertionError(f"unexpected rpc {method}")
    return fake


@pytest.fixture()
def api(tmp_path):
    _db(tmp_path, "usdc.db")
    for mod in [m for m in list(sys.modules) if m.startswith("dail.")]:
        del sys.modules[mod]
    import dail.api as api_mod
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    r = client.post("/agents", json={"id": "usdc_buyer", "name": "USDc Buyer"})
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    return client, api_mod, {"Authorization": f"Bearer {key}"}


def _intent(client, auth, amount=25, key="k1"):
    r = client.post("/payments/usdc/intent", headers=auth,
                    json={"agent_id": "usdc_buyer", "dail_amount": amount,
                          "idempotency_key": key})
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, auth, intent_id, tx_hash, key="c1"):
    return client.post("/payments/usdc/confirm", headers=auth,
                       json={"agent_id": "usdc_buyer", "intent_id": intent_id,
                             "tx_hash": tx_hash, "idempotency_key": key})


def _bal(client, auth):
    return client.get("/ledger/usdc_buyer", headers=auth).json()["balance"]


TX = "0x" + "ab" * 32


def test_status_public_and_configured(api):
    client, mod, _ = api
    r = client.get("/payments/usdc/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["deposit_address"] == TREASURY
    assert body["chain"] == "base" and body["redeemable"] is False


def test_happy_path_credits_1_to_1(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    assert intent["deposit_address"] == TREASURY
    before = _bal(client, auth)
    with patch.object(mod.usdc_payments, "_rpc", _rpc_factory(_receipt())):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duplicate"] is False and body["credited_dail"] == 25
    assert _bal(client, auth) == before + 25


def test_replay_same_tx_no_double_credit(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    with patch.object(mod.usdc_payments, "_rpc", _rpc_factory(_receipt())):
        r1 = _confirm(client, auth, intent["intent_id"], TX, key="c1")
        assert r1.json()["duplicate"] is False
        # Same tx submitted again (new intent): duplicate, no second credit.
        intent2 = _intent(client, auth, key="k2")
        r2 = _confirm(client, auth, intent2["intent_id"], TX, key="c2")
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    # 100 starter + 25 credited exactly once
    assert _bal(client, auth) == 125


def test_concurrent_confirm_same_tx_single_credit(api):
    client, mod, auth = api
    i1, i2 = _intent(client, auth, key="k1"), _intent(client, auth, key="k2")
    with patch.object(mod.usdc_payments, "_rpc", _rpc_factory(_receipt())):
        r1 = _confirm(client, auth, i1["intent_id"], TX, key="c1")
        r2 = _confirm(client, auth, i2["intent_id"], TX, key="c2")
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True
    assert _bal(client, auth) == 125


def test_lookalike_token_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    with patch.object(mod.usdc_payments, "_rpc",
                      _rpc_factory(_receipt(contract=FAKE_USDC))):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert "no USDC transfer" in r.json()["detail"]
    assert _bal(client, auth) == 100  # starter grant untouched


def test_transfer_to_wrong_address_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    with patch.object(mod.usdc_payments, "_rpc",
                      _rpc_factory(_receipt(to=OTHER))):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert _bal(client, auth) == 100


def test_failed_transaction_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    with patch.object(mod.usdc_payments, "_rpc",
                      _rpc_factory(_receipt(status="0x0"))):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert "failed" in r.json()["detail"]


def test_insufficient_confirmations_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    # latest 0x101 vs receipt 0x100 -> 1 confirmation < min 2
    with patch.object(mod.usdc_payments, "_rpc",
                      _rpc_factory(_receipt(), latest="0x101")):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert "confirmations" in r.json()["detail"]
    assert _bal(client, auth) == 100


def test_dust_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    with patch.object(mod.usdc_payments, "_rpc",
                      _rpc_factory(_receipt(units=500_000))):  # 0.5 USDC
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert "minimum" in r.json()["detail"]


def test_bad_tx_hash_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    r = _confirm(client, auth, intent["intent_id"], "notahash")
    assert r.status_code == 400


def test_intent_idempotent(api):
    client, _, auth = api
    a = _intent(client, auth, key="same")
    b = _intent(client, auth, key="same")
    assert a["intent_id"] == b["intent_id"]


def test_expired_intent_rejected(api):
    client, mod, auth = api
    intent = _intent(client, auth)
    # Backdate the intent beyond the 24h TTL directly in the DB.
    old = "2000-01-01T00:00:00+00:00"
    with mod.usdc_payments.engine.begin() as c:
        from sqlalchemy import text
        c.execute(text("UPDATE dail_usdc_deposits SET created_at=:o WHERE id=:i"),
                  {"o": old, "i": intent["intent_id"]})
    with patch.object(mod.usdc_payments, "_rpc", _rpc_factory(_receipt())):
        r = _confirm(client, auth, intent["intent_id"], TX)
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]


def test_wrong_agent_cannot_confirm(api):
    client, _, auth = api
    r = client.post("/agents", json={"id": "usdc_other", "name": "Other"})
    other_auth = {"Authorization": f"Bearer {r.json()['api_key']}"}
    intent = _intent(client, auth)
    r = client.post("/payments/usdc/confirm", headers=other_auth,
                    json={"agent_id": "usdc_other", "intent_id": intent["intent_id"],
                          "tx_hash": TX})
    assert r.status_code == 400
    assert "different agent" in r.json()["detail"]


def test_rpc_fallback_when_primary_flakes(api, tmp_path):
    """Primary RPC down -> falls back to the next URL instead of 400ing."""
    import io
    import urllib.request
    import dail.usdc_payments as up

    receipt = _receipt()
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
        def read(self):
            import json as j
            return j.dumps(self._payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    real_urlopen = urllib.request.urlopen

    def flaky(req, timeout=None):
        calls.append(req.full_url)
        if "mainnet.base.org" in req.full_url:
            raise IOError("simulated primary outage")
        method = __import__("json").loads(req.data)["method"]
        if method == "eth_getTransactionReceipt":
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": receipt})
        return _Resp({"jsonrpc": "2.0", "id": 1, "result": "0x110"})

    client, mod, auth = api
    intent = _intent(client, auth, key="fb1")
    before = _bal(client, auth)
    with patch.object(urllib.request, "urlopen", flaky):
        r = _confirm(client, auth, intent["intent_id"], TX, key="fbc1")
    assert r.status_code == 200, r.text
    assert r.json()["credited_dail"] == 25
    assert _bal(client, auth) == before + 25
    assert any("mainnet.base.org" in u for u in calls)
    assert any("mainnet.base.org" not in u for u in calls)
