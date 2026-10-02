"""x402 rail tests (self-hosted facilitator, Base USDC).

The chain and the facilitator broadcast are mocked at the seams:
- Facilitator.verify / Facilitator.settle are patched (no network, no gas).
- ChainVerifier.verify_usdc_to_treasury is patched (no Base RPC).
Facilitator EIP-712 crypto (recover_signer, calldata encoding) is tested
for real with eth_account-generated keys. No test touches a real chain.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TREASURY = "0xCd787bCf82279c121835EaaB37b34A502A6b8dBC"
USDC = to_checksum_address("0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913")
NETWORK = "eip155:8453"
FAKE_TX = "0x" + "ab" * 32

AUTH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}
DOMAIN = {"name": "USD Coin", "version": "2", "chainId": 8453,
          "verifyingContract": USDC}


def _db(tmp_path, name):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/{name}"


def make_client(tmp_path, **overrides):
    env = {
        "DAIL_X402_ENABLED": "true",
        "DAIL_USDC_TREASURY": TREASURY,
        "BASE_RPC_URL": "https://mainnet.base.org",
        "DAIL_USDC_MIN_CONF": "2",
        "X402_GAS_WALLET_KEY": Account.create().key.hex(),
        "X402_MIN_TOPUP_USDC": "1",
        "DAIL_ADMIN_KEY": "test-admin-key",
        "DAIL_TRADE_FEE_BPS": "1000",
    }
    env.update(overrides)
    for k, v in env.items():
        os.environ[k] = v
    _db(tmp_path, "x402.db")
    for mod in [m for m in list(sys.modules) if m.startswith("dail.")]:
        del sys.modules[mod]
    import dail.api as api_mod
    from fastapi.testclient import TestClient
    from conftest import fund_vault
    fund_vault(api_mod.dail)
    client = TestClient(api_mod.app)
    r = client.post("/agents", json={"id": "x402_buyer", "name": "X402 Buyer"})
    assert r.status_code == 200, r.text
    auth = {"Authorization": f"Bearer {r.json()['api_key']}"}
    return client, api_mod, auth


@pytest.fixture()
def api(tmp_path):
    return make_client(tmp_path)


def _signed_auth(payer_acct, to=TREASURY, usdc=25, valid_secs=120, nonce=None):
    now = int(time.time())
    auth = {
        "from": payer_acct.address,
        "to": to,
        "value": usdc * 10 ** 6,
        "validAfter": now - 10,
        "validBefore": now + valid_secs,
        "nonce": nonce or ("0x" + os.urandom(32).hex()),
    }
    encoded = encode_typed_data(full_message={
        "types": AUTH_TYPES, "domain": DOMAIN,
        "primaryType": "TransferWithAuthorization", "message": {
            "from": auth["from"], "to": auth["to"], "value": auth["value"],
            "validAfter": auth["validAfter"], "validBefore": auth["validBefore"],
            "nonce": bytes.fromhex(auth["nonce"][2:])}})
    signed = payer_acct.sign_message(encoded)
    return auth, "0x" + signed.signature.hex()


def _header(auth, signature, scheme="exact", network=NETWORK, version=2):
    payload = {"x402Version": version, "scheme": scheme, "network": network,
               "payload": {"signature": signature, "authorization": auth}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _topup(client, amount=25, header=None, agent="x402_buyer"):
    headers = {}
    if header:
        headers["PAYMENT-SIGNATURE"] = header
    return client.post("/payments/x402/topup", headers=headers,
                       json={"agent_id": agent, "usdc_amount": amount})


def _mock_success(api_mod, usdc=25, confs=5):
    """Facilitator settles, our own RPC confirms: the happy path."""
    return (
        patch.object(api_mod.x402_payments.facilitator, "verify",
                     return_value=(True, "")),
        patch.object(api_mod.x402_payments.facilitator, "settle",
                     return_value=FAKE_TX),
        patch.object(api_mod.x402_payments.chain, "verify_usdc_to_treasury",
                     return_value=(usdc * 10 ** 6, confs)),
    )


# ---- challenge / discovery ----

def test_challenge_shape(api):
    client, api_mod, hdrs = api
    r = _topup(client)
    assert r.status_code == 402, r.text
    assert "PAYMENT-REQUIRED" in r.headers
    challenge = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
    assert challenge["x402Version"] == 2
    req = challenge["accepts"][0]
    assert req["scheme"] == "exact"
    assert req["network"] == NETWORK
    assert req["asset"].lower() == USDC.lower()
    assert req["payTo"].lower() == TREASURY.lower()
    assert req["amount"] == str(25 * 10 ** 6)
    assert req["maxTimeoutSeconds"] == 300


def test_status_and_wellknown_public(api):
    client, api_mod, hdrs = api
    r = client.get("/payments/x402/status")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["rail"] == "x402" and s["configured"] is True
    assert s["facilitator"] == "self-hosted (in-process)"
    assert s["redeemable"] is False and s["dail_per_usdc"] == 1
    r = client.get("/.well-known/x402")
    assert r.status_code == 200, r.text
    assert r.json()["accepts"][0]["network"] == NETWORK


def test_disabled_rail_503(tmp_path):
    client, api_mod, _auth = make_client(tmp_path, DAIL_X402_ENABLED="false")
    r = _topup(client)
    assert r.status_code == 503, r.text


def test_missing_gas_key_503(tmp_path):
    client, api_mod, _auth = make_client(tmp_path, X402_GAS_WALLET_KEY="")
    r = client.get("/payments/x402/status")
    assert r.json()["configured"] is False
    r = _topup(client)
    assert r.status_code == 503, r.text


# ---- payload validation: every mismatch -> 402 again, never a credit ----

def _bad_payload_case(api, mutate):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    payload = {"x402Version": 2, "scheme": "exact", "network": NETWORK,
               "payload": {"signature": sig, "authorization": auth}}
    mutate(payload, auth)
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    with _mock_success(api_mod)[0], _mock_success(api_mod)[1], _mock_success(api_mod)[2]:
        r = _topup(client, header=header)
    assert r.status_code == 402, r.text
    assert "error" in r.json()
    # no credit happened
    bal = client.get("/ledger/x402_buyer", headers=hdrs).json()
    assert bal["balance"] == 100  # starter grant untouched


def test_reject_wrong_network(api):
    _bad_payload_case(api, lambda p, a: p.update(network="eip155:1"))


def test_reject_wrong_scheme(api):
    _bad_payload_case(api, lambda p, a: p.update(scheme="upto"))


def test_reject_wrong_payto(api):
    _bad_payload_case(
        api, lambda p, a: a.update(to="0x2222222222222222222222222222222222222222"))


def test_reject_wrong_amount(api):
    _bad_payload_case(api, lambda p, a: a.update(value=26 * 10 ** 6))


def test_reject_validbefore_too_far(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer, valid_secs=3600)
    header = _header(auth, sig)
    r = _topup(client, header=header)
    assert r.status_code == 402, r.text
    assert "300" in r.json()["error"]


def test_reject_malformed_header(api):
    client, api_mod, hdrs = api
    r = _topup(client, header="!!!not-base64!!!")
    assert r.status_code == 400, r.text


def test_reject_missing_signature_field(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    payload = {"x402Version": 2, "scheme": "exact", "network": NETWORK,
               "payload": {"authorization": auth}}  # no signature
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    r = _topup(client, header=header)
    assert r.status_code == 400, r.text


# ---- facilitator / chain failure modes: fail closed, no credit ----

def test_verify_invalid_no_settle_no_credit(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    header = _header(auth, sig)
    with patch.object(api_mod.x402_payments.facilitator, "verify",
                      return_value=(False, "payer USDC balance insufficient")) as v, \
         patch.object(api_mod.x402_payments.facilitator, "settle") as s:
        r = _topup(client, header=header)
    assert r.status_code == 402, r.text
    assert "insufficient" in r.json()["error"]
    s.assert_not_called()
    assert client.get("/ledger/x402_buyer", headers=hdrs).json()["balance"] == 100


def test_settle_failure_no_credit(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    header = _header(auth, sig)
    from dail.x402_facilitator import FacilitatorError
    with patch.object(api_mod.x402_payments.facilitator, "verify",
                      return_value=(True, "")), \
         patch.object(api_mod.x402_payments.facilitator, "settle",
                      side_effect=FacilitatorError("broadcast failed")):
        r = _topup(client, header=header)
    assert r.status_code == 502, r.text
    assert client.get("/ledger/x402_buyer", headers=hdrs).json()["balance"] == 100


def test_no_credit_without_onchain_confirm(api):
    """Settle succeeded but our own RPC can't prove it: fail closed."""
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    header = _header(auth, sig)
    from dail.chain_verify import ChainError
    v, s, _ = _mock_success(api_mod)
    with v, s, patch.object(
            api_mod.x402_payments.chain, "verify_usdc_to_treasury",
            side_effect=ChainError("no USDC transfer to the DAiL deposit address")):
        r = _topup(client, header=header)
    assert r.status_code == 502, r.text
    assert client.get("/ledger/x402_buyer", headers=hdrs).json()["balance"] == 100


def test_banned_agent_refused(api):
    client, api_mod, hdrs = api
    api_mod.dail.ban_agent("x402_buyer", reason="test")
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    r = _topup(client, header=_header(auth, sig))
    assert r.status_code == 400, r.text
    assert "banned" in r.text


# ---- happy path + idempotency ----

def test_happy_path_credits_exactly(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer, usdc=25)
    header = _header(auth, sig)
    v, s, c = _mock_success(api_mod, usdc=25)
    with v, s, c:
        r = _topup(client, header=header)
    assert r.status_code == 200, r.text
    assert "PAYMENT-RESPONSE" in r.headers
    resp = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert resp["transaction"] == FAKE_TX and resp["network"] == NETWORK
    body = r.json()
    assert body["credited_dail"] == 25 and body["duplicate"] is False
    assert client.get("/ledger/x402_buyer", headers=hdrs).json()["balance"] == 125
    # ledger idempotency key
    txs = api_mod.dail.ledger.transactions
    assert any(t.idempotency_key == f"x402:{FAKE_TX}" and t.kind == "x402_deposit"
               for t in txs.values())
    # row marked paid
    pays = api_mod.x402_payments.list_payments()
    assert pays[0]["status"] == "paid" and pays[0]["settle_tx_hash"] == FAKE_TX


def test_replay_same_signature_single_credit(api):
    client, api_mod, hdrs = api
    payer = Account.create()
    auth, sig = _signed_auth(payer, usdc=10)
    header = _header(auth, sig)
    v, s, c = _mock_success(api_mod, usdc=10)
    with v, s, c:
        r1 = _topup(client, amount=10, header=header)
        r2 = _topup(client, amount=10, header=header)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert client.get("/ledger/x402_buyer", headers=hdrs).json()["balance"] == 110  # once only


# ---- facilitator crypto unit tests (real signing, mocked chain reads) ----

def _fac_chain(rpc_map, sim=None):
    """Facilitator with a fake chain. rpc_map handles diagnostic reads
    (authorizationState/balanceOf); sim controls the settle simulation —
    None means success, an Exception means the simulation raises it."""
    from dail import x402_facilitator as xf
    from dail.chain_verify import ChainVerifier

    chain = ChainVerifier(TREASURY, ["https://mainnet.base.org"], 2)
    chain._rpc = lambda method, params: rpc_map(method, params)
    if sim is None:
        chain.simulate_call = lambda call: "0x" + "00" * 32
    else:
        def _sim(call):
            if isinstance(sim, Exception):
                raise sim
            return sim
        chain.simulate_call = _sim
    return xf.Facilitator(chain, Account.create().key.hex())


def test_recover_signer_roundtrip():
    from dail import x402_facilitator as xf
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    auth["signature"] = sig
    assert xf.recover_signer(auth).lower() == payer.address.lower()


def test_facilitator_verify_ok_and_reasons():
    from dail import x402_facilitator as xf
    from dail.chain_verify import ChainRevert
    payer = Account.create()
    auth, sig = _signed_auth(payer, usdc=5)
    auth["signature"] = sig

    def rpc_ok(method, params):
        data = params[0]["data"]
        if data.startswith("0x" + xf._AUTH_STATE_SELECTOR):
            return "0x" + "00" * 32  # nonce unused (diagnostic path)
        if data.startswith("0x" + xf._BALANCE_OF_SELECTOR):
            return hex(5 * 10 ** 6)  # exactly funded
        raise AssertionError(method)

    f = _fac_chain(rpc_ok)  # simulation succeeds
    assert f.verify(auth) == (True, "")

    def rpc_used(method, params):
        data = params[0]["data"]
        if data.startswith("0x" + xf._AUTH_STATE_SELECTOR):
            return "0x" + "00" * 31 + "01"  # nonce already used
        raise AssertionError(method)

    f2 = _fac_chain(rpc_used, sim=ChainRevert("reverted"))
    ok, reason = f2.verify(auth)
    assert not ok and "nonce" in reason

    def rpc_poor(method, params):
        data = params[0]["data"]
        if data.startswith("0x" + xf._AUTH_STATE_SELECTOR):
            return "0x" + "00" * 32
        if data.startswith("0x" + xf._BALANCE_OF_SELECTOR):
            return hex(1)  # nearly empty
        raise AssertionError(method)

    f3 = _fac_chain(rpc_poor, sim=ChainRevert("reverted"))
    ok, reason = f3.verify(auth)
    assert not ok and "balance" in reason

    # revert with no diagnostic info -> generic message, still fail closed
    def rpc_broken(method, params):
        raise AssertionError(method)

    f4 = _fac_chain(rpc_broken, sim=ChainRevert("reverted"))
    ok, reason = f4.verify(auth)
    assert not ok and "simulation" in reason

    # chain unreachable (transport failure, not a revert) -> fail closed
    from dail.chain_verify import ChainError

    def rpc_down(method, params):
        raise ChainError("base_rpc_unreachable")

    f5 = _fac_chain(rpc_down, sim=ChainError("base_rpc_unreachable"))
    ok, reason = f5.verify(auth)
    assert not ok and "unreachable" in reason


def test_facilitator_verify_wrong_signer():
    from dail import x402_facilitator as xf
    payer = Account.create()
    other = Account.create()
    auth, sig = _signed_auth(payer)
    auth["signature"] = sig
    auth["from"] = other.address  # tampered payer field
    f = _fac_chain(lambda m, p: "0x" + "00" * 32)
    ok, reason = f.verify(auth)
    assert not ok and "not from payer" in reason


def test_calldata_shape():
    from dail import x402_facilitator as xf
    payer = Account.create()
    auth, sig = _signed_auth(payer)
    auth["signature"] = sig
    data = xf.encode_authorization_calldata(auth)
    assert data.startswith("0x" + xf._TRANSFER_WITH_AUTH_SELECTOR)
    # selector (4) + head (6 x 32) + bytes offset (32) + bytes len (32)
    # + 65-byte signature right-padded to 96
    assert len(data) == 2 + (4 + 6 * 32 + 32 + 32 + 96) * 2
    assert data.endswith(sig[2:] + "0" * 62)  # packed r||s||v, padded
