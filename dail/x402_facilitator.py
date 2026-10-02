"""Self-hosted x402 facilitator (in-process).

Implements the facilitator role from the x402 v2 spec — verify and settle
semantics for the `exact` EVM scheme — as plain Python functions called
in-process by the x402 top-up rail.

Deliberately NOT exposed over HTTP: the gas wallet must never become a
public settlement service. Anyone able to reach a public /settle could
grief our gas balance by submitting arbitrary authorizations. The only
client of this facilitator is our own top-up route.

Trust model (same as any facilitator, minus the third party):
- verify() never moves funds. It checks the EIP-3009 authorization is
  well-formed, correctly signed by the payer for exactly our
  requirements, unexpired, unused, and funded — via eth_call simulation
  against the real USDC contract plus local EIP-712 recovery.
- settle() broadcasts transferWithAuthorization from the gas wallet and
  returns the tx hash. The rail then confirms the receipt through our own
  ChainVerifier before crediting a single DAIL: we never credit on the
  facilitator's word alone, even when the facilitator is us.

Gas wallet ops:
- X402_GAS_WALLET_KEY: hex private key of a dedicated Base EOA. Fund it
  with a few dollars of ETH (Base settlement is ~$0.001-0.002/tx; $5
  covers thousands of top-ups). The wallet holds no USDC and never
  receives customer funds — worst case if the key leaks, the attacker
  drains the small ETH balance. Rotate by funding a new key and swapping
  the env var.
"""
import time

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from .chain_verify import (BASE_CHAIN_ID, BASE_USDC_CONTRACT,
                           ChainError, ChainRevert)

# EIP-712 domain for Circle's native USDC on Base (FiatTokenV2_2).
# (Checksummed: the ABI encoder rejects non-checksummed addresses.)
EIP712_DOMAIN = {
    "name": "USD Coin",
    "version": "2",
    "chainId": BASE_CHAIN_ID,
    "verifyingContract": to_checksum_address(BASE_USDC_CONTRACT.lower()),
}
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
# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)
# — verified against Circle's FiatTokenV2_2 source (circlefin/stablecoin-evm):
# the signature is PACKED BYTES (r‖s‖v, 65 bytes), not a (v,r,s) triple.
# If a future USDC upgrade changes this, settle fails closed (simulation
# reverts) — re-verify the ABI against the canonical source before mainnet.
_TRANSFER_WITH_AUTH_SELECTOR = keccak(
    b"transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)"
)[:4].hex()
# authorizationState(address,bytes32) -> bool ; balanceOf(address) -> uint256
_AUTH_STATE_SELECTOR = keccak(b"authorizationState(address,bytes32)")[:4].hex()
_BALANCE_OF_SELECTOR = keccak(b"balanceOf(address)")[:4].hex()


class FacilitatorError(ValueError):
    """Facilitator-level failure (agent-facing)."""


class FacilitatorNotReady(RuntimeError):
    """Gas wallet not configured (maps to HTTP 503)."""


def _addr(a):
    a = (a or "").strip()
    if not (a.startswith("0x") and len(a) == 42):
        raise FacilitatorError(f"bad address: {a!r}"[:80])
    return to_checksum_address(a)


def _b32(h):
    h = (h or "").strip()
    if not (h.startswith("0x") and len(h) == 66):
        raise FacilitatorError("bad bytes32")
    return bytes.fromhex(h[2:])


def _sig_bytes(auth):
    sig = (auth.get("signature") or "").strip()
    if not (sig.startswith("0x") and len(sig) == 132):
        raise FacilitatorError("bad signature encoding (want 0x + 65 bytes)")
    return bytes.fromhex(sig[2:])


def encode_authorization_calldata(auth):
    """Calldata for USDC.transferWithAuthorization(...). The signature is
    the 65-byte packed r‖s‖v from the x402 payload."""
    return ("0x" + _TRANSFER_WITH_AUTH_SELECTOR + encode(
        ["address", "address", "uint256", "uint256", "uint256",
         "bytes32", "bytes"],
        [_addr(auth["from"]), _addr(auth["to"]), int(auth["value"]),
         int(auth["validAfter"]), int(auth["validBefore"]),
         _b32(auth["nonce"]), _sig_bytes(auth)]).hex())


def recover_signer(auth):
    """Recover the EIP-712 signer of the authorization. Raises on bad sig."""
    msg = {
        "from": _addr(auth["from"]), "to": _addr(auth["to"]),
        "value": int(auth["value"]), "validAfter": int(auth["validAfter"]),
        "validBefore": int(auth["validBefore"]), "nonce": _b32(auth["nonce"]),
    }
    encoded = encode_typed_data(full_message={
        "types": AUTH_TYPES, "domain": EIP712_DOMAIN,
        "primaryType": "TransferWithAuthorization", "message": msg})
    sig = (auth.get("signature") or "").strip()
    if not (sig.startswith("0x") and len(sig) == 132):
        raise FacilitatorError("bad signature encoding")
    try:
        # Account.recover_message re-applies the EIP-712 "\x19\x01" hashing
        # internally — the same path sign_message used — so recovery is
        # consistent with any compliant wallet signature.
        return Account.recover_message(encoded, signature=bytes.fromhex(sig[2:]))
    except Exception as e:
        raise FacilitatorError(f"signature recovery failed: {e}"[:120])


class Facilitator:
    def __init__(self, chain, gas_key=None):
        self.chain = chain
        gas_key = (gas_key or "").strip()
        self._account = Account.from_key(gas_key) if gas_key else None

    @property
    def ready(self):
        return self.chain.ready and self._account is not None

    def _require_ready(self):
        if not self.ready:
            raise FacilitatorNotReady(
                "x402_settlement_not_configured: fund X402_GAS_WALLET_KEY")

    @property
    def gas_address(self):
        return self._account.address if self._account else None

    def verify(self, auth):
        """Returns (True, '') when the authorization would settle cleanly;
        (False, reason) otherwise. Never moves funds.

        Two layers: (1) local checks — well-formed fields, time window,
        EIP-712 signature recovers to the payer; (2) an eth_call simulation
        of the exact settle calldata, which is authoritative: the chain
        validates the signature against USDC's real EIP-712 domain and
        checks nonce-unused, payer-funded, token-unpaused, and neither
        party blacklisted. The verdict never depends on our own copy of
        the domain — only the diagnostic messages do.
        """
        now = int(time.time())
        try:
            valid_after = int(auth["validAfter"])
            valid_before = int(auth["validBefore"])
            value = int(auth["value"])
        except (KeyError, TypeError, ValueError):
            return False, "malformed authorization fields"
        if not (valid_after <= now <= valid_before):
            return False, "authorization not currently valid"
        if value <= 0:
            return False, "zero value"
        if valid_before - now > 3600:
            return False, "authorization window too wide"
        try:
            payer = _addr(auth["from"])
            signer = recover_signer(auth)
        except FacilitatorError as e:
            return False, str(e)
        if signer.lower() != payer.lower():
            return False, "signature not from payer"
        try:
            calldata = encode_authorization_calldata(auth)
        except FacilitatorError as e:
            return False, str(e)
        gas_addr = self.gas_address or payer
        try:
            self.chain.simulate_call({
                "from": gas_addr, "to": BASE_USDC_CONTRACT, "data": calldata})
            return True, ""
        except ChainRevert:
            return False, self._diagnose_revert(auth, payer, value)
        except ChainError as e:
            return False, f"chain unreachable: {e}"[:120]

    def _diagnose_revert(self, auth, payer, value):
        """Best-effort reason for a simulation revert (diagnostic only —
        the verdict is already 'no')."""
        try:
            used = self.chain._rpc("eth_call", [{
                "to": BASE_USDC_CONTRACT,
                "data": "0x" + _AUTH_STATE_SELECTOR + encode(
                    ["address", "bytes32"],
                    [payer, _b32(auth["nonce"])]).hex(),
            }, "latest"])
            if used and int(used, 16) != 0:
                return "nonce already used"
        except Exception:
            pass
        try:
            bal = self.chain._rpc("eth_call", [{
                "to": BASE_USDC_CONTRACT,
                "data": "0x" + _BALANCE_OF_SELECTOR + encode(
                    ["address"], [payer]).hex(),
            }, "latest"])
            if not bal or int(bal, 16) < value:
                return "payer USDC balance insufficient"
        except Exception:
            pass
        return ("authorization rejected by simulation "
                "(bad signature or contract state)")

    def settle(self, auth):
        """Broadcast transferWithAuthorization from the gas wallet.

        Returns the tx hash hex. Raises FacilitatorError / ChainError /
        FacilitatorNotReady on any failure (fail closed: no hash, no credit).
        """
        self._require_ready()
        ok, reason = self.verify(auth)
        if not ok:
            raise FacilitatorError(f"settle refused: {reason}"[:160])
        calldata = encode_authorization_calldata(auth)
        gas_addr = self._account.address
        try:
            gas_price = int(self.chain._rpc("eth_gasPrice", []), 16)
            tx_count = int(self.chain._rpc("eth_getTransactionCount",
                                           [gas_addr, "pending"]), 16)
        except ChainError as e:
            raise FacilitatorError(f"settle chain read failed: {e}"[:140])
        tx = {
            "to": BASE_USDC_CONTRACT,
            "data": calldata,
            "gas": 120000,
            "gasPrice": gas_price,
            "nonce": tx_count,
            "chainId": BASE_CHAIN_ID,
            "value": 0,
        }
        try:
            gas_est = self.chain._rpc("eth_estimateGas", [{
                "from": gas_addr, "to": BASE_USDC_CONTRACT, "data": calldata}])
            tx["gas"] = min(int(gas_est, 16) + 20000, 300000)
        except Exception:
            pass  # fall back to the 120k cap; a revert surfaces below
        # Final simulation from the gas wallet: catches wrong-ABI and
        # wrong-signer problems before we spend gas, and races where the
        # nonce got consumed between verify() and now.
        try:
            self.chain.simulate_call({
                "from": gas_addr, "to": BASE_USDC_CONTRACT, "data": calldata})
        except ChainRevert as e:
            raise FacilitatorError(
                f"settle simulation reverted (authorization likely just used): "
                f"{e}"[:160])
        signed = self._account.sign_transaction(tx)
        tx_hash = self.chain._rpc(
            "eth_sendRawTransaction", [signed.raw_transaction.hex()])
        if not tx_hash:
            raise FacilitatorError("broadcast returned no hash")
        return tx_hash
